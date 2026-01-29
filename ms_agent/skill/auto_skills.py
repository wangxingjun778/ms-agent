# flake8: noqa
# isort: skip_file
# yapf: disable
import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import json
from ms_agent.llm import LLM
from ms_agent.llm.utils import Message
from ms_agent.retriever.hybrid_retriever import HybridRetriever
from ms_agent.skill.container import (ExecutionInput, ExecutionOutput,
                                      ExecutorType, SkillContainer)
from ms_agent.skill.loader import load_skills
from ms_agent.skill.prompts import (PROMPT_ANALYZE_QUERY_FOR_SKILLS,
                                    PROMPT_BUILD_SKILLS_DAG,
                                    PROMPT_CLARIFY_USER_INTENT,
                                    PROMPT_DIRECT_SELECT_SKILLS,
                                    PROMPT_EVALUATE_SKILLS_COMPLETENESS,
                                    PROMPT_SKILL_ANALYSIS_PLAN,
                                    PROMPT_SKILL_EXECUTION_COMMAND)
from ms_agent.skill.schema import SkillContext, SkillExecutionPlan, SkillSchema
from ms_agent.utils.logger import get_logger

logger = get_logger()


@dataclass
class SkillExecutionResult:
    """
    Result of executing a single skill.

    Attributes:
        skill_id: Identifier of the executed skill.
        success: Whether execution was successful.
        output: ExecutionOutput from container.
        error: Error message if execution failed.
    """
    skill_id: str
    success: bool = False
    output: Optional[ExecutionOutput] = None
    error: Optional[str] = None


@dataclass
class DAGExecutionResult:
    """
    Result of executing the entire skill DAG.

    Attributes:
        success: Whether all skills executed successfully.
        results: Dict mapping skill_id to SkillExecutionResult.
        execution_order: Actual execution order (with parallel groups).
        total_duration_ms: Total execution duration in milliseconds.
    """
    success: bool = False
    results: Dict[str, SkillExecutionResult] = field(default_factory=dict)
    execution_order: List[Union[str, List[str]]] = field(default_factory=list)
    total_duration_ms: float = 0.0

    def get_skill_output(self, skill_id: str) -> Optional[ExecutionOutput]:
        """Get output from a specific skill execution."""
        result = self.results.get(skill_id)
        return result.output if result else None


class SkillAnalyzer:
    """
    Progressive skill analyzer for incremental context loading.

    Implements two-phase analysis:
    1. Plan Phase: Analyze skill metadata + content to create execution plan
    2. Load Phase: Load only required resources based on plan
    """

    def __init__(self, llm: 'LLM'):
        """
        Initialize skill analyzer.

        Args:
            llm: LLM instance for analysis.
        """
        self.llm = llm

    def _llm_generate(self, prompt: str) -> str:
        """Generate LLM response from prompt."""
        from ms_agent.llm.utils import Message
        messages = [Message(role='user', content=prompt)]
        response = self.llm.generate(messages=messages)
        return response.content if hasattr(response,
                                           'content') else str(response)

    def _parse_json_response(self, response: str) -> Dict[str, Any]:
        """Parse JSON from LLM response."""
        import re
        response = re.sub(r'```json\s*', '', response)
        response = re.sub(r'```\s*', '', response)
        try:
            return json.loads(response.strip())
        except json.JSONDecodeError:
            logger.warning(f'Failed to parse JSON: {response[:200]}')
            return {}

    def analyze_skill_plan(self,
                           skill: SkillSchema,
                           query: str,
                           root_path: Path = None) -> SkillContext:
        """
        Phase 1: Analyze skill and create execution plan.

        Only loads skill metadata and content (SKILL.md), not scripts/resources.

        Args:
            skill: SkillSchema to analyze.
            query: User's query to fulfill.
            root_path: Root path for skill context.

        Returns:
            SkillContext with execution plan (resources not yet loaded).
        """
        # Create context with lazy loading
        context = SkillContext(
            skill=skill,
            query=query,
            root_path=root_path or skill.skill_path.parent)

        # Build prompt with skill overview (not full content)
        prompt = PROMPT_SKILL_ANALYSIS_PLAN.format(
            query=query,
            skill_id=skill.skill_id,
            skill_name=skill.name,
            skill_description=skill.description,
            skill_content=skill.content[:4000] if skill.content else '',
            scripts_list=', '.join(context.get_scripts_list()) or 'None',
            references_list=', '.join(context.get_references_list()) or 'None',
            resources_list=', '.join(context.get_resources_list()) or 'None')

        response = self._llm_generate(prompt)
        parsed = self._parse_json_response(response)

        # Build execution plan
        plan = SkillExecutionPlan(
            can_handle=parsed.get('can_handle', False),
            plan_summary=parsed.get('plan_summary', ''),
            steps=parsed.get('steps', []),
            required_scripts=parsed.get('required_scripts', []),
            required_references=parsed.get('required_references', []),
            required_resources=parsed.get('required_resources', []),
            parameters=parsed.get('parameters', {}),
            reasoning=parsed.get('reasoning', ''))

        context.plan = plan
        context.spec.plan = plan.plan_summary

        logger.info(
            f'Skill analysis plan: can_handle={plan.can_handle}, '
            f'scripts={plan.required_scripts}, refs={plan.required_references}'
        )

        return context

    def load_skill_resources(self, context: SkillContext) -> SkillContext:
        """
        Phase 2: Load resources based on execution plan.

        Args:
            context: SkillContext with plan from Phase 1.

        Returns:
            SkillContext with loaded resources.
        """
        if not context.plan or not context.plan.can_handle:
            logger.warning('No valid plan, skipping resource loading')
            return context

        context.load_from_plan()
        logger.info(
            f'Loaded resources: scripts={len(context.scripts)}, '
            f'refs={len(context.references)}, res={len(context.resources)}')

        return context

    def generate_execution_commands(
            self, context: SkillContext) -> List[Dict[str, Any]]:
        """
        Generate execution commands from loaded context.

        Args:
            context: SkillContext with loaded resources.

        Returns:
            List of execution command dictionaries.
        """
        if not context.plan:
            return []

        prompt = PROMPT_SKILL_EXECUTION_COMMAND.format(
            query=context.query,
            skill_id=context.skill.skill_id,
            execution_plan=json.dumps(
                {
                    'plan_summary': context.plan.plan_summary,
                    'steps': context.plan.steps,
                    'parameters': context.plan.parameters,
                },
                indent=2),
            scripts_content=context.get_loaded_scripts_content(),
            references_content=context.get_loaded_references_content()[:2000],
            resources_content=context.get_loaded_resources_content()[:2000])

        response = self._llm_generate(prompt)
        parsed = self._parse_json_response(response)

        commands = parsed.get('commands', [])
        context.spec.tasks = json.dumps(commands, indent=2)

        return commands

    async def analyze_and_prepare(
            self,
            skill: SkillSchema,
            query: str,
            root_path: Path = None
    ) -> Tuple[SkillContext, List[Dict[str, Any]]]:
        """
        Complete progressive analysis: plan -> load -> generate commands.

        Args:
            skill: SkillSchema to analyze.
            query: User's query.
            root_path: Root path for context.

        Returns:
            Tuple of (SkillContext, execution_commands).
        """
        # Phase 1: Create plan
        context = await asyncio.to_thread(self.analyze_skill_plan, skill,
                                          query, root_path)

        if not context.plan or not context.plan.can_handle:
            return context, []

        # Phase 2: Load resources
        await asyncio.to_thread(self.load_skill_resources, context)

        # Phase 3: Generate commands
        commands = await asyncio.to_thread(self.generate_execution_commands,
                                           context)

        return context, commands


@dataclass
class SkillDAGResult:
    """
    Result of AutoSkills run containing the skill execution DAG.

    Attributes:
        dag: Adjacency list representation of skill dependencies.
        execution_order: Topologically sorted list of skill_ids (sublists = parallel).
        selected_skills: Dict of selected SkillSchema objects.
        is_complete: Whether the skills are sufficient for the task.
        clarification: Optional clarification question if skills are insufficient.
        chat_response: Direct response if no skills needed (chat-only mode).
        execution_result: Result of DAG execution (populated after execute_dag).
    """
    dag: Dict[str, List[str]] = field(default_factory=dict)
    execution_order: List[Union[str, List[str]]] = field(default_factory=list)
    selected_skills: Dict[str, SkillSchema] = field(default_factory=dict)
    is_complete: bool = False
    clarification: Optional[str] = None
    chat_response: Optional[str] = None
    execution_result: Optional[DAGExecutionResult] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert SkillDAGResult to dictionary."""
        return {
            'dag':
            self.dag,
            'execution_order':
            self.execution_order,
            'selected_skills':
            {k: v.__dict__
             for k, v in self.selected_skills.items()},
            'is_complete':
            self.is_complete,
            'clarification':
            self.clarification,
            'chat_response':
            self.chat_response,
            'execution_result':
            self.execution_result.__dict__ if self.execution_result else None,
        }


class DAGExecutor:
    """
    Executor for skill DAG with dependency-aware parallel execution.

    Handles execution order parsing, input/output linking between skills,
    and parallel execution of independent skills.
    Supports progressive skill analysis for incremental context loading.
    """

    def __init__(self,
                 container: SkillContainer,
                 skills: Dict[str, SkillSchema],
                 workspace_dir: Optional[Path] = None,
                 llm: 'LLM' = None,
                 enable_progressive_analysis: bool = True):
        """
        Initialize DAG executor.

        Args:
            container: SkillContainer for executing skills.
            skills: Dict of skill_id to SkillSchema.
            workspace_dir: Optional workspace directory for skill execution.
            llm: LLM instance for progressive skill analysis.
            enable_progressive_analysis: Whether to use progressive analysis.
        """
        self.container = container
        self.skills = skills
        self.workspace_dir = workspace_dir or container.workspace_dir
        self.llm = llm
        self.enable_progressive_analysis = enable_progressive_analysis and llm is not None

        # Skill analyzer for progressive analysis
        self._analyzer: Optional[SkillAnalyzer] = None
        if self.enable_progressive_analysis:
            self._analyzer = SkillAnalyzer(llm)

        # Execution state: stores outputs keyed by skill_id
        self._outputs: Dict[str, ExecutionOutput] = {}

        # Skill contexts from progressive analysis
        self._contexts: Dict[str, SkillContext] = {}

    def _get_skill_dependencies(self, skill_id: str,
                                dag: Dict[str, List[str]]) -> List[str]:
        """
        Get direct dependencies of a skill from the DAG.

        Args:
            skill_id: The skill to get dependencies for.
            dag: Adjacency list where dag[A] = [B, C] means A depends on B, C.

        Returns:
            List of skill_ids that this skill depends on.
        """
        return dag.get(skill_id, [])

    def _build_execution_input(
            self,
            skill_id: str,
            dag: Dict[str, List[str]],
            user_input: Optional[ExecutionInput] = None) -> ExecutionInput:
        """
        Build execution input for a skill, linking outputs from dependencies.

        Args:
            skill_id: The skill to build input for.
            dag: Skill dependency DAG.
            user_input: Optional user-provided input.

        Returns:
            ExecutionInput with linked dependency outputs.
        """
        base_input = user_input or ExecutionInput()

        # Get outputs from upstream dependencies
        dependencies = self._get_skill_dependencies(skill_id, dag)
        upstream_data: Dict[str, Any] = {}

        for dep_id in dependencies:
            if dep_id in self._outputs:
                dep_output = self._outputs[dep_id]
                # Pass stdout/return_value as upstream data
                upstream_data[dep_id] = {
                    'stdout': dep_output.stdout,
                    'stderr': dep_output.stderr,
                    'return_value': dep_output.return_value,
                    'exit_code': dep_output.exit_code,
                    'output_files':
                    {k: str(v)
                     for k, v in dep_output.output_files.items()},
                }

        # Inject upstream data into environment variables as JSON
        env_vars = base_input.env_vars.copy()
        if upstream_data:
            env_vars['UPSTREAM_OUTPUTS'] = json.dumps(upstream_data)
            # Also provide individual upstream references
            for dep_id, data in upstream_data.items():
                safe_key = dep_id.replace('-', '_').replace('.', '_').upper()
                if data.get('stdout'):
                    env_vars[f'UPSTREAM_{safe_key}_STDOUT'] = data[
                        'stdout'][:4096]

        return ExecutionInput(
            args=base_input.args,
            kwargs=base_input.kwargs,
            env_vars=env_vars,
            input_files=base_input.input_files,
            stdin=base_input.stdin,
            working_dir=base_input.working_dir,
            requirements=base_input.requirements,
        )

    def _determine_executor_type(self, skill: SkillSchema) -> ExecutorType:
        """
        Determine the executor type based on skill scripts.

        Args:
            skill: SkillSchema to analyze.

        Returns:
            ExecutorType for the skill's primary script.
        """
        if not skill.scripts:
            return ExecutorType.PYTHON_CODE

        # Check first script's extension
        primary_script = skill.scripts[0]
        ext = primary_script.type.lower()

        if ext in ['.py']:
            return ExecutorType.PYTHON_SCRIPT
        elif ext in ['.sh', '.bash']:
            return ExecutorType.SHELL
        elif ext in ['.js', '.mjs']:
            return ExecutorType.JAVASCRIPT
        else:
            return ExecutorType.PYTHON_CODE

    async def _execute_single_skill(
            self,
            skill_id: str,
            dag: Dict[str, List[str]],
            user_input: Optional[ExecutionInput] = None,
            query: str = '') -> SkillExecutionResult:
        """
        Execute a single skill with dependency-linked input.

        Uses progressive analysis if enabled:
        1. Analyze skill to create execution plan
        2. Load only required resources
        3. Generate and execute commands

        Args:
            skill_id: ID of the skill to execute.
            dag: Skill dependency DAG.
            user_input: Optional user-provided input.
            query: User query for progressive analysis.

        Returns:
            SkillExecutionResult with execution outcome.
        """
        skill = self.skills.get(skill_id)
        if not skill:
            return SkillExecutionResult(
                skill_id=skill_id,
                success=False,
                error=f'Skill not found: {skill_id}')

        try:
            # Build base input with upstream outputs
            exec_input = self._build_execution_input(skill_id, dag, user_input)

            # Use progressive analysis if enabled
            if self.enable_progressive_analysis and self._analyzer:
                return await self._execute_with_progressive_analysis(
                    skill, skill_id, exec_input, query)

            # Fallback: direct execution without progressive analysis
            return await self._execute_direct(skill, skill_id, exec_input)

        except Exception as e:
            logger.error(f'Skill execution failed for {skill_id}: {e}')
            return SkillExecutionResult(
                skill_id=skill_id, success=False, error=str(e))

    async def _execute_with_progressive_analysis(
            self, skill: SkillSchema, skill_id: str,
            exec_input: ExecutionInput, query: str) -> SkillExecutionResult:
        """
        Execute skill using progressive analysis.

        Args:
            skill: SkillSchema to execute.
            skill_id: Skill identifier.
            exec_input: Execution input with upstream data.
            query: User query for context.

        Returns:
            SkillExecutionResult with execution outcome.
        """
        # Phase 1 & 2: Analyze and load resources
        context, commands = await self._analyzer.analyze_and_prepare(
            skill, query, self.workspace_dir)

        # Store context for reference
        self._contexts[skill_id] = context

        if not context.plan or not context.plan.can_handle:
            return SkillExecutionResult(
                skill_id=skill_id,
                success=False,
                error=
                f'Skill cannot handle query: {context.plan.reasoning if context.plan else "No plan"}'
            )

        if not commands:
            return SkillExecutionResult(
                skill_id=skill_id,
                success=False,
                error='No execution commands generated')

        # Phase 3: Execute commands
        outputs: List[ExecutionOutput] = []
        for cmd in commands:
            cmd_type = cmd.get('type', 'python_code')
            output = await self._execute_command(cmd, cmd_type, skill_id,
                                                 exec_input, context)
            outputs.append(output)

            if output.exit_code != 0:
                # Stop on first failure
                break

        # Merge outputs
        final_output = self._merge_outputs(outputs)

        # Store output for downstream skills
        self._outputs[skill_id] = final_output
        self.container.spec.link_upstream(skill_id, final_output)

        return SkillExecutionResult(
            skill_id=skill_id,
            success=(final_output.exit_code == 0),
            output=final_output,
            error=final_output.stderr if final_output.exit_code != 0 else None)

    async def _execute_direct(
            self, skill: SkillSchema, skill_id: str,
            exec_input: ExecutionInput) -> SkillExecutionResult:
        """
        Execute skill directly without progressive analysis.

        Args:
            skill: SkillSchema to execute.
            skill_id: Skill identifier.
            exec_input: Execution input.

        Returns:
            SkillExecutionResult with execution outcome.
        """
        executor_type = self._determine_executor_type(skill)

        if skill.scripts:
            script_path = skill.scripts[0].path
            output = await self.container.execute(
                executor_type=executor_type,
                skill_id=skill_id,
                script_path=script_path,
                input_spec=exec_input)
        else:
            output = await self.container.execute_python_code(
                code=skill.content or '# No executable content',
                skill_id=skill_id,
                input_spec=exec_input)

        self._outputs[skill_id] = output
        self.container.spec.link_upstream(skill_id, output)

        return SkillExecutionResult(
            skill_id=skill_id,
            success=(output.exit_code == 0),
            output=output,
            error=output.stderr if output.exit_code != 0 else None)

    async def _execute_command(self, cmd: Dict[str, Any], cmd_type: str,
                               skill_id: str, exec_input: ExecutionInput,
                               context: SkillContext) -> ExecutionOutput:
        """
        Execute a single command from progressive analysis.

        Args:
            cmd: Command dictionary.
            cmd_type: Type of command (python_script, shell, etc.).
            skill_id: Skill identifier.
            exec_input: Base execution input.
            context: SkillContext with loaded resources.

        Returns:
            ExecutionOutput from command execution.
        """
        # Merge parameters into input
        params = cmd.get('parameters', {})
        merged_input = ExecutionInput(
            args=exec_input.args + list(params.values()),
            kwargs={
                **exec_input.kwargs,
                **params
            },
            env_vars={
                **exec_input.env_vars,
                **{k.upper(): str(v)
                   for k, v in params.items()}
            },
            input_files=exec_input.input_files,
            stdin=exec_input.stdin,
            working_dir=exec_input.working_dir or context.root_path,
            requirements=cmd.get('requirements', []) + exec_input.requirements)

        if cmd_type == 'python_script':
            script_path = cmd.get('path')
            if script_path:
                # Resolve path relative to skill
                full_path = context.skill.skill_path / script_path
                if not full_path.exists():
                    full_path = context.root_path / script_path
                return await self.container.execute_python_script(
                    script_path=full_path,
                    skill_id=skill_id,
                    input_spec=merged_input)
            else:
                code = cmd.get('code', '')
                return await self.container.execute_python_code(
                    code=code, skill_id=skill_id, input_spec=merged_input)

        elif cmd_type == 'python_code':
            code = cmd.get('code', '')
            return await self.container.execute_python_code(
                code=code, skill_id=skill_id, input_spec=merged_input)

        elif cmd_type == 'shell':
            command = cmd.get('code') or cmd.get('command', '')
            return await self.container.execute_shell(
                command=command, skill_id=skill_id, input_spec=merged_input)

        elif cmd_type == 'javascript':
            code = cmd.get('code', '')
            return await self.container.execute_javascript(
                code=code, skill_id=skill_id, input_spec=merged_input)

        else:
            # Default to python code
            code = cmd.get('code', '')
            return await self.container.execute_python_code(
                code=code, skill_id=skill_id, input_spec=merged_input)

    def _merge_outputs(self,
                       outputs: List[ExecutionOutput]) -> ExecutionOutput:
        """Merge multiple execution outputs into one."""
        if not outputs:
            return ExecutionOutput()
        if len(outputs) == 1:
            return outputs[0]

        # Merge all outputs
        merged_stdout = '\n'.join(o.stdout for o in outputs if o.stdout)
        merged_stderr = '\n'.join(o.stderr for o in outputs if o.stderr)
        final_exit_code = next(
            (o.exit_code for o in outputs if o.exit_code != 0), 0)
        total_duration = sum(o.duration_ms for o in outputs)

        # Merge output files
        merged_files = {}
        for o in outputs:
            merged_files.update(o.output_files)

        return ExecutionOutput(
            stdout=merged_stdout,
            stderr=merged_stderr,
            exit_code=final_exit_code,
            output_files=merged_files,
            duration_ms=total_duration)

    async def _execute_parallel_group(
            self,
            skill_ids: List[str],
            dag: Dict[str, List[str]],
            user_input: Optional[ExecutionInput] = None,
            query: str = '') -> List[SkillExecutionResult]:
        """
        Execute a group of skills in parallel.

        Args:
            skill_ids: List of skill_ids to execute concurrently.
            dag: Skill dependency DAG.
            user_input: Optional user-provided input.
            query: User query for progressive analysis.

        Returns:
            List of SkillExecutionResult for each skill.
        """
        tasks = [
            self._execute_single_skill(sid, dag, user_input, query)
            for sid in skill_ids
        ]
        return await asyncio.gather(*tasks)

    async def execute(self,
                      dag: Dict[str, List[str]],
                      execution_order: List[Union[str, List[str]]],
                      user_input: Optional[ExecutionInput] = None,
                      stop_on_failure: bool = True,
                      query: str = '') -> DAGExecutionResult:
        """
        Execute the skill DAG according to execution order.

        Execution order format: [skill1, skill2, [skill3, skill4], skill5, ...]
        - Single string items are executed sequentially
        - List items (sublists) are executed in parallel

        Args:
            dag: Skill dependency DAG (adjacency list).
            execution_order: Ordered list with parallel groups as sublists.
            user_input: Optional initial input for all skills.
            stop_on_failure: Whether to stop execution on first failure.
            query: User query for progressive skill analysis.

        Returns:
            DAGExecutionResult with all execution outcomes.
        """
        import time
        start_time = time.time()

        results: Dict[str, SkillExecutionResult] = {}
        actual_order: List[Union[str, List[str]]] = []
        all_success = True

        for item in execution_order:
            if isinstance(item, list):
                # Parallel execution group
                group_results = await self._execute_parallel_group(
                    item, dag, user_input, query)
                for res in group_results:
                    results[res.skill_id] = res
                    if not res.success:
                        all_success = False
                actual_order.append(item)

                if not all_success and stop_on_failure:
                    logger.warning(
                        f'Stopping DAG execution due to failure in parallel group: {item}'
                    )
                    break
            else:
                # Sequential execution
                result = await self._execute_single_skill(
                    item, dag, user_input, query)
                results[result.skill_id] = result
                actual_order.append(item)

                if not result.success:
                    all_success = False
                    if stop_on_failure:
                        logger.warning(
                            f'Stopping DAG execution due to failure: {item}')
                        break

        total_duration = (time.time() - start_time) * 1000

        return DAGExecutionResult(
            success=all_success,
            results=results,
            execution_order=actual_order,
            total_duration_ms=total_duration)

    def get_skill_context(self, skill_id: str) -> Optional[SkillContext]:
        """Get the skill context from progressive analysis."""
        return self._contexts.get(skill_id)


class AutoSkills:
    """
    Automatic skill retrieval and DAG construction for user queries.

    Uses hybrid retrieval (dense + sparse) to find relevant skills,
    with LLM-based analysis and reflection loop for completeness checking.
    Supports DAG-based skill execution with dependency management.
    """

    def __init__(self,
                 skills: Union[str, List[str], List[SkillSchema]],
                 llm: LLM,
                 enable_search: Union[bool, None] = None,
                 enable_intent_clarification: bool = True,
                 top_k: int = 3,
                 min_score: float = 0.7,
                 max_iterations: int = 3,
                 work_dir: Optional[Union[str, Path]] = None,
                 use_sandbox: bool = True,
                 **kwargs):
        """
        Initialize AutoSkills with skills corpus and retriever.

        Args:
            skills: Path(s) to skill directories or list of SkillSchema.
            llm: LLM instance for query analysis and evaluation.
            enable_search: If True, use HybridRetriever for skill search.
                If False, put all skills into LLM context for direct selection.
                If None, enable search only if skills > 10 automatically.
            enable_intent_clarification: If True, verify user intent before
                execution and prompt for clarification if needed.
            top_k: Number of top results to retrieve per query.
            min_score: Minimum score threshold for retrieval.
            max_iterations: Maximum reflection loop iterations.
            work_dir: Working directory for skill execution.
            use_sandbox: Whether to use Docker sandbox for execution.
        """
        # Dict of <skill_id, SkillSchema>
        self.all_skills: Dict[str, SkillSchema] = load_skills(skills=skills)
        logger.info(f'Loaded {len(self.all_skills)} skills from {skills}')

        self.llm = llm
        self.enable_search = len(
            self.all_skills) > 10 if enable_search is None else enable_search
        self.enable_intent_clarification = enable_intent_clarification
        self.top_k = top_k
        self.min_score = min_score
        self.max_iterations = max_iterations
        self.work_dir = Path(work_dir) if work_dir else None
        self.use_sandbox = use_sandbox
        self.kwargs = kwargs

        # Build corpus and skill_id mapping
        self.corpus: List[str] = []
        self.corpus_to_skill_id: Dict[str, str] = {}
        self._build_corpus()

        # Initialize retriever only if search is enabled
        self.retriever: Optional[HybridRetriever] = None
        if self.enable_search and self.corpus:
            self.retriever = HybridRetriever(corpus=self.corpus, **kwargs)

        # Container and executor (lazy initialization)
        self._container: Optional[SkillContainer] = None
        self._executor: Optional[DAGExecutor] = None

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

    def _clarify_user_intent(
        self, query: str, dag_result: SkillDAGResult
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Clarify user intent by analyzing if selected skills satisfy the query.

        Args:
            query: User's original query.
            dag_result: SkillDAGResult containing selected skills.

        Returns:
            Tuple of (intent_satisfied, clarification_question, suggestion).
        """
        if not dag_result.selected_skills:
            return (
                False,
                'No skills were selected. Could you provide more details about what you want to achieve?',
                None)

        # Format selected skills with name and description
        skills_info = []
        for skill_id, skill in dag_result.selected_skills.items():
            skills_info.append(
                f'- [{skill_id}] {skill.name}\n  Description: {skill.description}'
            )
        selected_skills_text = '\n'.join(skills_info)

        prompt = PROMPT_CLARIFY_USER_INTENT.format(
            query=query, selected_skills=selected_skills_text)
        response = self._llm_generate(prompt)
        parsed = self._parse_json_response(response)

        intent_satisfied = parsed.get('intent_satisfied', False)
        confidence = parsed.get('confidence', 0.0)
        clarification = parsed.get('clarification_needed')
        suggestion = parsed.get('suggestion')

        # Log analysis results
        logger.info(
            f'Intent clarification: satisfied={intent_satisfied}, confidence={confidence}'
        )

        if not intent_satisfied or confidence < 0.8:
            coverage = parsed.get('coverage_analysis', {})
            missing = coverage.get('missing', [])
            if missing:
                logger.info(f'Missing capabilities: {missing}')
            return False, clarification, suggestion

        return True, None, None

    async def _async_clarify_user_intent(
        self, query: str, dag_result: SkillDAGResult
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Async wrapper for _clarify_user_intent."""
        return await asyncio.to_thread(self._clarify_user_intent, query,
                                       dag_result)

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

    async def get_skill_dag(self, query: str) -> SkillDAGResult:
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

        skills_dag: Dict[str, Any] = dag_result.get('dag', {})
        execution_order: List[str] = dag_result.get('execution_order', [])
        logger.info(
            f'Final DAG built with skills: {skills_dag}, execution order: {execution_order}'
        )

        return SkillDAGResult(
            dag=skills_dag,
            execution_order=execution_order,
            selected_skills=selected,
            is_complete=(clarification is None),
            clarification=clarification)

    def _get_container(self) -> SkillContainer:
        """Get or create SkillContainer instance."""
        if self._container is None:
            self._container = SkillContainer(
                workspace_dir=self.work_dir,
                use_sandbox=self.use_sandbox,
                **{
                    k: v
                    for k, v in self.kwargs.items() if k in [
                        'timeout', 'image', 'memory_limit',
                        'enable_security_check', 'network_enabled'
                    ]
                })
        return self._container

    def _get_executor(self) -> DAGExecutor:
        """Get or create DAGExecutor instance."""
        if self._executor is None:
            container = self._get_container()
            self._executor = DAGExecutor(
                container=container,
                skills=self.all_skills,
                workspace_dir=self.work_dir,
                llm=self.llm,
                enable_progressive_analysis=True)
        return self._executor

    async def execute_dag(self,
                          dag_result: SkillDAGResult,
                          user_input: Optional[ExecutionInput] = None,
                          stop_on_failure: bool = True,
                          query: str = '') -> DAGExecutionResult:
        """
        Execute the skill DAG from a SkillDAGResult.

        Executes skills according to the execution_order, handling:
        - Sequential execution for single skill items
        - Parallel execution for skill groups (sublists)
        - Input/output linking between dependent skills
        - Progressive skill analysis (plan -> load -> execute)

        Args:
            dag_result: SkillDAGResult containing DAG and execution order.
            user_input: Optional initial input for skills.
            stop_on_failure: Whether to stop on first failure.
            query: User query for progressive skill analysis.

        Returns:
            DAGExecutionResult with all execution outcomes.
        """
        if not dag_result.is_complete:
            logger.warning('DAG is not complete, execution may fail')

        if not dag_result.execution_order:
            logger.warning('Empty execution order, nothing to execute')
            return DAGExecutionResult(success=True)

        executor = self._get_executor()
        result = await executor.execute(
            dag=dag_result.dag,
            execution_order=dag_result.execution_order,
            user_input=user_input,
            stop_on_failure=stop_on_failure,
            query=query)

        # Attach result to dag_result for convenience
        dag_result.execution_result = result

        logger.info(f'DAG execution completed: success={result.success}, '
                    f'duration={result.total_duration_ms:.2f}ms')

        return result

    def get_execution_spec(self) -> Optional[str]:
        """Get the execution spec log as markdown string."""
        if self._container:
            return self._container.get_spec_log()
        return None

    def save_execution_spec(self,
                            output_path: Optional[Union[str, Path]] = None):
        """Save the execution spec to a markdown file."""
        if self._container:
            self._container.save_spec_log(output_path)

    def cleanup(self, keep_spec: bool = True):
        """Clean up container workspace."""
        if self._container:
            self._container.cleanup(keep_spec=keep_spec)

    async def run(
            self,
            query: str,
            user_input: Optional[ExecutionInput] = None,
            stop_on_failure: bool = True,
            enable_intent_clarification: Optional[bool] = None
    ) -> SkillDAGResult:
        """
        Run skill retrieval and execute the resulting DAG in one call.

        Combines get_skill_dag(), intent clarification, and execute_dag().
        Uses progressive skill analysis for each skill execution.

        Args:
            query: User's task query.
            user_input: Optional initial input for skills.
            stop_on_failure: Whether to stop on first failure.
            enable_intent_clarification: Whether to verify intent before execution.
                If None, uses the instance-level setting.

        Returns:
            SkillDAGResult with execution_result populated.
        """
        dag_result = await self.get_skill_dag(query)

        # Skip execution for chat-only results
        if dag_result.chat_response:
            logger.info('Chat-only response, skipping execution')
            return dag_result

        # Skip if skills are incomplete
        if not dag_result.is_complete:
            logger.warning(f'Skills incomplete: {dag_result.clarification}')
            return dag_result

        # Use instance setting if not explicitly provided
        do_clarification = enable_intent_clarification \
            if enable_intent_clarification is not None else self.enable_intent_clarification

        # Intent clarification step
        if do_clarification and dag_result.selected_skills:
            intent_satisfied, clarification, suggestion = await self._async_clarify_user_intent(
                query, dag_result)

            if not intent_satisfied:
                # Build clarification message
                clarification_msg = clarification or 'The selected skills may not fully address your needs.'
                if suggestion:
                    clarification_msg += f'\n\nSuggestion: {suggestion}'

                logger.info(
                    f'Intent clarification needed: {clarification_msg}')
                print(f'\n[Clarification Needed]\n{clarification_msg}\n')

                # Mark result as incomplete and return for user input
                dag_result.is_complete = False
                dag_result.clarification = clarification_msg
                return dag_result

        # Execute the DAG
        if dag_result.execution_order:
            await self.execute_dag(
                dag_result, user_input, stop_on_failure, query=query)

        return dag_result

    async def run_with_clarification(
            self,
            initial_query: str,
            additional_info: str = '',
            user_input: Optional[ExecutionInput] = None,
            stop_on_failure: bool = True) -> SkillDAGResult:
        """
        Run with support for iterative intent clarification.

        Use this method when the user provides additional information
        after a clarification request.

        Args:
            initial_query: Original user query.
            additional_info: Additional information from user clarification.
            user_input: Optional initial input for skills.
            stop_on_failure: Whether to stop on first failure.

        Returns:
            SkillDAGResult with execution_result populated.
        """
        # Combine queries if additional info provided
        if additional_info:
            combined_query = f'{initial_query}\n\nAdditional context: {additional_info}'
        else:
            combined_query = initial_query

        return await self.run(
            query=combined_query,
            user_input=user_input,
            stop_on_failure=stop_on_failure,
            enable_intent_clarification=None)
