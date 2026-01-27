import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import json
from ms_agent.llm import LLM
from ms_agent.llm.utils import Message
from ms_agent.retriever.hybrid_retriever import HybridRetriever
from ms_agent.skill.loader import load_skills
from ms_agent.skill.prompts import (PROMPT_ANALYZE_QUERY_FOR_SKILLS,
                                    PROMPT_BUILD_SKILLS_DAG,
                                    PROMPT_DIRECT_SELECT_SKILLS,
                                    PROMPT_EVALUATE_SKILLS_COMPLETENESS)
from ms_agent.skill.schema import SkillSchema
from ms_agent.utils.logger import get_logger

logger = get_logger()


@dataclass
class SkillDAGResult:
    """
    Result of AutoSkills run containing the skill execution DAG.

    Attributes:
        dag: Adjacency list representation of skill dependencies.
        execution_order: Topologically sorted list of skill_ids.
        selected_skills: Dict of selected SkillSchema objects.
        is_complete: Whether the skills are sufficient for the task.
        clarification: Optional clarification question if skills are insufficient.
        chat_response: Direct response if no skills needed (chat-only mode).
    """
    dag: Dict[str, List[str]] = field(default_factory=dict)
    execution_order: List[str] = field(default_factory=list)
    selected_skills: Dict[str, SkillSchema] = field(default_factory=dict)
    is_complete: bool = False
    clarification: Optional[str] = None
    chat_response: Optional[str] = None


class AutoSkills:
    """
    Automatic skill retrieval and DAG construction for user queries.

    Uses hybrid retrieval (dense + sparse) to find relevant skills,
    with LLM-based analysis and reflection loop for completeness checking.
    """

    def __init__(self,
                 skills: Union[str, List[str], List[SkillSchema]],
                 llm: LLM,
                 enable_search: bool = True,
                 top_k: int = 3,
                 min_score: float = 0.7,
                 max_iterations: int = 3,
                 **kwargs):
        """
        Initialize AutoSkills with skills corpus and retriever.

        Args:
            skills: Path(s) to skill directories or list of SkillSchema.
            llm: LLM instance for query analysis and evaluation.
            enable_search: If True, use HybridRetriever for skill search.
                If False, put all skills into LLM context for direct selection.
            top_k: Number of top results to retrieve per query.
            min_score: Minimum score threshold for retrieval.
            max_iterations: Maximum reflection loop iterations.
        """
        # Dict of <skill_id, SkillSchema>
        self.all_skills: Dict[str, SkillSchema] = load_skills(skills=skills)
        logger.info(f'Loaded {len(self.all_skills)} skills from {skills}')

        self.llm = llm
        self.enable_search = enable_search
        self.top_k = top_k
        self.min_score = min_score
        self.max_iterations = max_iterations
        self.kwargs = kwargs

        # Build corpus and skill_id mapping
        self.corpus: List[str] = []
        self.corpus_to_skill_id: Dict[str, str] = {}
        self._build_corpus()

        # Initialize retriever only if search is enabled
        self.retriever: Optional[HybridRetriever] = None
        if self.enable_search and self.corpus:
            self.retriever = HybridRetriever(corpus=self.corpus, **kwargs)

    def _build_corpus(self):
        """Build corpus from skills for retriever indexing."""
        for skill_id, skill in self.all_skills.items():
            # Concatenate skill_id, name, description as corpus document
            doc = f'[{skill_id}] {skill.name}: {skill.description}'
            self.corpus.append(doc)
            self.corpus_to_skill_id[doc] = skill_id

    def _extract_skill_id_from_doc(self, doc: str) -> Optional[str]:
        """Extract skill_id from corpus document string."""
        # First try direct lookup
        if doc in self.corpus_to_skill_id:
            return self.corpus_to_skill_id[doc]
        # Fallback: extract from [skill_id] pattern
        match = re.match(r'\[([^\]]+)\]', doc)
        return match.group(1) if match else None

    def _parse_json_response(self, response: str) -> Dict[str, Any]:
        """Parse JSON from LLM response, handling markdown code blocks."""
        # Remove markdown code blocks if present
        response = re.sub(r'```json\s*', '', response)
        response = re.sub(r'```\s*', '', response)
        try:
            return json.loads(response.strip())
        except json.JSONDecodeError:
            logger.warning(f'Failed to parse JSON response: {response[:200]}')
            return {}

    def _get_skills_overview(self, limit: int = 20) -> str:
        """Generate a brief overview of all available skills."""
        lines = []
        for skill_id, skill in self.all_skills.items():
            lines.append(
                f'- [{skill_id}] {skill.name}: {skill.description[:100]}')
        return '\n'.join(lines[:limit])  # Limit to avoid token overflow

    def _get_all_skills_context(self) -> str:
        """Generate full context of all skills for direct LLM selection."""
        lines = []
        for skill_id, skill in self.all_skills.items():
            lines.append(f'- [{skill_id}] {skill.name}\n  {skill.description}')
        return '\n'.join(lines)

    def _format_retrieved_skills(self, skill_ids: Set[str]) -> str:
        """Format retrieved skills for LLM prompt."""
        lines = []
        for skill_id in skill_ids:
            if skill_id in self.all_skills:
                skill = self.all_skills[skill_id]
                lines.append(
                    f'- [{skill_id}] {skill.name}\n  {skill.description}')
        return '\n'.join(lines)

    def _llm_generate(self, prompt: str) -> str:
        """Generate LLM response from prompt."""
        messages = [Message(role='user', content=prompt)]
        response = self.llm.generate(messages=messages)
        return response.content if hasattr(response,
                                           'content') else str(response)

    async def _async_llm_generate(self, prompt: str) -> str:
        """Async wrapper for LLM generation."""
        return await asyncio.to_thread(self._llm_generate, prompt)

    def _analyze_query(
        self,
        query: str,
    ) -> Tuple[bool, str, List[str], Optional[str]]:
        """
        Analyze user query to determine if skills are needed.

        Args:
            query: User's original query.

        Returns:
            Tuple of (needs_skills, intent_summary, skill_queries, chat_response).
        """
        prompt = PROMPT_ANALYZE_QUERY_FOR_SKILLS.format(
            query=query, skills_overview=self._get_skills_overview())
        response = self._llm_generate(prompt)
        parsed = self._parse_json_response(response)

        needs_skills = parsed.get('needs_skills', True)
        intent = parsed.get('intent_summary', query)
        queries = parsed.get('skill_queries', [query])
        chat_response = parsed.get('chat_response')
        return needs_skills, intent, queries if queries else [query
                                                              ], chat_response

    async def _async_retrieve_skills(self, queries: List[str]) -> Set[str]:
        """
        Retrieve skills for multiple queries in parallel.

        Args:
            queries: List of search queries.

        Returns:
            Set of unique skill_ids from all queries.
        """
        if not self.retriever:
            return set()

        # Run parallel async searches
        tasks = [
            self.retriever.async_search(
                query=q, top_k=self.top_k, min_score=self.min_score)
            for q in queries
        ]
        results = await asyncio.gather(*tasks)

        # Collect unique skill_ids
        skill_ids = set()
        for result_list in results:
            for doc, score in result_list:
                skill_id = self._extract_skill_id_from_doc(doc)
                if skill_id:
                    skill_ids.add(skill_id)
        return skill_ids

    def _evaluate_completeness(
            self, query: str, intent: str,
            skill_ids: Set[str]) -> Tuple[bool, List[str], Optional[str]]:
        """
        Evaluate if retrieved skills are complete for the task.

        Args:
            query: Original user query.
            intent: Summarized intent from analysis.
            skill_ids: Set of retrieved skill_ids.

        Returns:
            Tuple of (is_complete, additional_queries, clarification_question).
        """
        prompt = PROMPT_EVALUATE_SKILLS_COMPLETENESS.format(
            query=query,
            intent_summary=intent,
            retrieved_skills=self._format_retrieved_skills(skill_ids))
        response = self._llm_generate(prompt)
        parsed = self._parse_json_response(response)

        is_complete = parsed.get('is_complete', True)
        additional = parsed.get('additional_queries', [])
        clarification = parsed.get('clarification_needed')
        return is_complete, additional, clarification

    def _build_dag(self, query: str, skill_ids: Set[str]) -> Dict[str, Any]:
        """
        Build execution DAG from selected skills.

        Args:
            query: Original user query.
            skill_ids: Set of skill_ids to include in DAG.

        Returns:
            Dict containing 'dag' and 'execution_order'.
        """
        skills_info = self._format_retrieved_skills(skill_ids)
        prompt = PROMPT_BUILD_SKILLS_DAG.format(
            query=query, selected_skills=skills_info)
        response = self._llm_generate(prompt)
        parsed = self._parse_json_response(response)

        dag = parsed.get('dag', {skill_id: [] for skill_id in skill_ids})
        order = parsed.get('execution_order', list(skill_ids))
        return {'dag': dag, 'execution_order': order}

    def _direct_select_skills(self, query: str) -> SkillDAGResult:
        """
        Directly select skills using LLM with all skills in context.

        Used when enable_search=False. Puts all skills into LLM context
        and lets LLM select relevant skills and build DAG in one call.

        Args:
            query: User's task query.

        Returns:
            SkillDAGResult containing the skill execution DAG.
        """
        prompt = PROMPT_DIRECT_SELECT_SKILLS.format(
            query=query, all_skills=self._get_all_skills_context())
        response = self._llm_generate(prompt)
        parsed = self._parse_json_response(response)

        # Handle chat-only response
        needs_skills = parsed.get('needs_skills', True)
        chat_response = parsed.get('chat_response')

        if not needs_skills:
            logger.info('Chat-only query, no skills needed')
            if chat_response:
                print(f'\n[Chat Response]\n{chat_response}\n')
            return SkillDAGResult(
                is_complete=True, chat_response=chat_response)

        # Extract selected skills and DAG
        selected_ids = parsed.get('selected_skill_ids', [])
        dag = parsed.get('dag', {})
        order = parsed.get('execution_order', [])

        # Validate skill_ids exist
        valid_ids = {sid for sid in selected_ids if sid in self.all_skills}
        selected = {sid: self.all_skills[sid] for sid in valid_ids}

        logger.info(f'Direct selection: {valid_ids}')

        return SkillDAGResult(
            dag=dag,
            execution_order=order,
            selected_skills=selected,
            is_complete=bool(valid_ids),
            clarification=None if valid_ids else 'No relevant skills found.')

    async def run(self, query: str) -> SkillDAGResult:
        """
        Run the autonomous skill retrieval and DAG construction loop.

        Iteratively retrieves skills, evaluates completeness with reflection,
        and builds execution DAG. Loop terminates when:
        - Query is chat-only (no skills needed)
        - Max iterations reached
        - Skills are deemed complete for the task
        - Clarification from user is needed

        Args:
            query: User's task query.

        Returns:
            SkillDAGResult containing the skill execution DAG.
        """
        if not self.all_skills:
            logger.warning('No skills loaded, returning empty result')
            return SkillDAGResult()

        # Direct selection mode: put all skills into LLM context
        if not self.enable_search:
            logger.info('Direct selection mode (enable_search=False)')
            return self._direct_select_skills(query)

        # Search mode: use HybridRetriever
        if not self.retriever:
            logger.warning('Retriever not initialized, returning empty result')
            return SkillDAGResult()

        # Step 1: Analyze query to determine if skills are needed
        needs_skills, intent, skill_queries, chat_response = self._analyze_query(
            query)
        logger.info(f'Needs skills: {needs_skills}, Intent: {intent}')

        # If chat-only, return empty DAG with chat response
        if not needs_skills:
            logger.info('Chat-only query, no skills needed')
            if chat_response:
                print(f'\n[Chat Response]\n{chat_response}\n')
            return SkillDAGResult(
                is_complete=True, chat_response=chat_response)

        logger.info(f'Skill queries: {skill_queries}')

        collected_skills: Set[str] = set()
        clarification: Optional[str] = None

        # Step 2: Reflection loop
        for iteration in range(self.max_iterations):
            logger.info(f'Iteration {iteration + 1}/{self.max_iterations}')

            # Retrieve skills for current queries
            new_skills = await self._async_retrieve_skills(skill_queries)
            collected_skills.update(new_skills)
            logger.info(
                f'Retrieved skills: {new_skills}, Total: {collected_skills}')

            if not collected_skills:
                clarification = 'No relevant skills found. Please provide more details.'
                break

            # Evaluate completeness
            is_complete, additional_queries, clarification = self._evaluate_completeness(
                query, intent, collected_skills)

            if is_complete:
                logger.info('Skills are complete for the task')
                break

            if clarification:
                logger.info(f'Clarification needed: {clarification}')
                break

            if not additional_queries:
                logger.info('No additional queries, stopping iteration')
                break

            # Continue with additional queries
            skill_queries = additional_queries
            logger.info(
                f'Additional queries for next iteration: {skill_queries}')

        # Step 3: Build DAG from collected skills
        dag_result = self._build_dag(query, collected_skills)

        # Construct result
        selected = {
            sid: self.all_skills[sid]
            for sid in collected_skills if sid in self.all_skills
        }

        return SkillDAGResult(
            dag=dag_result.get('dag', {}),
            execution_order=dag_result.get('execution_order', []),
            selected_skills=selected,
            is_complete=(clarification is None),
            clarification=clarification)

    def run_sync(self, query: str) -> SkillDAGResult:
        """Synchronous wrapper for run()."""
        return asyncio.run(self.run(query))
