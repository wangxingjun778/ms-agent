# Copyright (c) Alibaba, Inc. and its affiliates.
import os.path
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import json
from ms_agent.llm import Message
from ms_agent.llm.openai_llm import OpenAI as OpenAILLM
from ms_agent.utils.logger import logger
from ms_agent.utils.utils import extract_by_tag, install_package, str_to_md5
from omegaconf import DictConfig, OmegaConf

from .loader import load_skills
from .prompts import (PROMPT_SKILL_PLAN, PROMPT_SKILL_TASKS,
                      PROMPT_TASKS_IMPLEMENTATION,
                      SCRIPTS_IMPLEMENTATION_FORMAT)
from .retrieve import create_retriever
from .schema import SkillContext, SkillSchema


class AgentSkill:
    """
    LLM Agent with progressive skill loading mechanism.

    Implements a multi-level progressive context loading and processing mechanism:
        1. Level 1 (Metadata): Load all skill names and descriptions
        2. Level 2 (Retrieval): Retrieve and load SKILL.md when relevant with the query
        3. Level 3 (Resources): Load additional files (references, scripts, resources) only when referenced in SKILL.md
        4. Level 4 (Analysis and Execution): Analyze the loaded skill context and execute scripts as needed
    """

    def __init__(
        self,
        skills: Union[str, List[str], List[SkillSchema]],
        model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        stream: Optional[bool] = True,
        enable_thinking: Optional[bool] = False,
        max_tokens: Optional[int] = 8192,
        work_dir: str = None,
        use_sandbox: bool = False,
    ):
        """
        Initialize Agent Skills.

        Args:
            skills: Path(s) to skill directories,
                the root path of skill directories, list of SkillSchema, or skill IDs on the hub
                Note: skill IDs on the hub are not yet implemented.
            api_key: OpenAI API key
            base_url: Custom API base URL
            model: LLM model name
            stream: Whether to stream responses
            work_dir: Working directory.
            use_sandbox: Whether to use sandbox environment for script execution.
                If True, scripts will be executed in the `ms-enclave` sandbox environment.
                If False, scripts will be executed directly in the local environment.
        """
        self.work_dir: Path = Path(work_dir) if work_dir else Path.cwd()
        os.makedirs(self.work_dir, exist_ok=True)

        self.stream: bool = stream
        self.use_sandbox: bool = use_sandbox

        # Preprocess skills
        skills = self._preprocess_skills(skills=skills)

        # Pre-load all skills, the key is "skill_id@version"
        self.all_skills: Dict[str, SkillSchema] = load_skills(skills=skills)
        logger.info(f'Loaded {len(self.all_skills)} skills from {skills}')

        # Initialize retriever
        self.retriever = create_retriever(skills=self.all_skills, )

        # Initialize OpenAI client
        api_key = api_key or os.getenv('OPENAI_API_KEY')
        base_url = base_url or os.getenv('OPENAI_BASE_URL')
        _conf: DictConfig = OmegaConf.create({
            'llm': {
                'model': model,
                'openai_base_url': base_url,
                'openai_api_key': api_key,
            },
            'generation_config': {
                'stream': stream,
                'extra_body': {
                    'enable_thinking': enable_thinking,
                },
                'max_tokens': max_tokens,
            }
        })

        self.llm = OpenAILLM(_conf)

        # Initialize sandbox environment
        if self.use_sandbox:
            # TODO: to be implemented
            ...

        # Conversation history
        self.conversation_history: List[Dict[str, str]] = []

        # Loaded skill contexts (Level 2 & 3)
        self.loaded_contexts: Dict[str, Dict[str, Any]] = {}

        logger.info('Agent Skills initialized successfully')

    def _preprocess_skills(
        self, skills: Union[str, List[str], List[SkillSchema]]
    ) -> Union[str, List[str], List[SkillSchema]]:
        """
        Preprocess skills by copying them to the working directory.

        Args:
            skills: Path(s) to skill directories,
                the root path of skill directories, list of SkillSchema, or skill IDs on the hub

        Returns:
            Processed skills in the working directory.
        """
        results: Union[str, List[str], List[SkillSchema]] = []

        if isinstance(skills, str):
            skills = [skills]

        if skills is None or len(skills) == 0:
            return results

        if isinstance(skills[0], SkillSchema):
            return skills

        skill_paths: List[str] = self._find_skill_dir(skills)

        skill_root_in_workdir: str = str(self.work_dir / '.cache/skills')
        for skill_path in skill_paths:
            path_in_workdir = os.path.join(skill_root_in_workdir,
                                           Path(skill_path).name)
            if os.path.exists(path_in_workdir):
                shutil.rmtree(path_in_workdir, ignore_errors=True)
            shutil.copytree(skill_path, path_in_workdir)

        return results

    def _init_sandbox(self):
        # TODO: to be implemented

        # Check and install the `ms-enclave` sandbox framework
        package_name: str = 'ms-enclave'
        import_name: str = 'ms_enclave'
        try:
            logger.info(f'Installing sandbox package: {package_name}...')
            install_package(package_name=package_name, import_name=import_name)
        except Exception as e:
            raise RuntimeError(
                f'Failed to install `{package_name}` package: {str(e)}')

        ...

    def _build_skill_context(self, skill: SkillSchema) -> SkillContext:

        skill_context: SkillContext = SkillContext(
            skill=skill,
            work_dir=self.work_dir,
        )

        return skill_context

    def _call_llm(self,
                  user_prompt: str,
                  system_prompt: str = None,
                  stream: bool = True) -> str:

        default_system: str = 'Your are an intelligent assistant that can help users by leveraging specialized skills.'
        system_prompt = system_prompt or default_system

        messages = [
            Message(role='assistant', content=system_prompt),
            Message(role='user', content=user_prompt),
        ]
        resp = self.llm.generate(
            messages=messages,
            stream=stream,
        )

        _content = ''
        is_first = True
        _response_message = None
        for _response_message in resp:
            if is_first:
                messages.append(_response_message)
                is_first = False
            new_content = _response_message.content[len(_content):]
            sys.stdout.write(new_content)
            sys.stdout.flush()
            _content = _response_message.content
            messages[-1] = _response_message
        sys.stdout.write('\n')

        return _content

    def run(self, query: str) -> str:
        """
        Run the agent skill with the given query.

        Args:
            query: User query string

        Returns:
            Agent response string
        """
        logger.info(
            f'Received user query: {query}, starting skill retrieval...')
        # Retrieve relevant skills
        relevant_skills = self.retriever.retrieve(
            query=query,
            method='semantic',
            top_k=5,
        )
        logger.debug(
            f'Retrieved {len(relevant_skills)} relevant skills for query')

        if not relevant_skills:
            logger.warning('No relevant skills found')
            logger.error(
                "I couldn't find any relevant skills for your query. Could you please rephrase or provide more details?"
            )
            return ''

        # Use the most relevant skill
        # TODO: Support multiple skills collaboration
        top_skill_key, top_skill, score = relevant_skills[0]
        logger.info(f'Using skill: {top_skill_key} (score: {score:.2f})')
        skill: SkillSchema = top_skill

        # Build skill context
        skill_context: SkillContext = self._build_skill_context(skill)
        skill_md_context: str = '\n\n<!-- SKILL_MD_CONTEXT -->\n' + skill_context.skill.content.strip(
        )
        reference_context: str = '\n\n<!-- REFERENCE_CONTEXT -->\n' + '\n'.join(
            [
                json.dumps(ref.pop('file'), ensure_ascii=False)
                for ref in skill_context.references
            ])
        script_context: str = '\n\n<!-- SCRIPT_CONTEXT -->\n' + '\n'.join([
            json.dumps(script.pop('file'), ensure_ascii=False)
            for script in skill_context.scripts
        ])
        resource_context: str = '\n\n<!-- RESOURCE_CONTEXT -->\n' + '\n'.join([
            json.dumps(res.pop('file'), ensure_ascii=False)
            for res in skill_context.resources
        ])

        # PLAN: Analyse the SKILL.md, references, and scripts.
        prompt_skill_plan: str = PROMPT_SKILL_PLAN.format(
            query=query,
            skill_md_context=skill_md_context,
            reference_context=reference_context,
            script_context=script_context,
            resource_context=resource_context,
        )

        response_skill_plan = self._call_llm(
            user_prompt=prompt_skill_plan,
            stream=self.stream,
        )
        skill_context.spec.plan = response_skill_plan
        logger.info('\n======== Completed Skill Plan Response ========\n')

        # TASKS: Get solutions and tasks based on analysis.
        prompt_skill_tasks: str = PROMPT_SKILL_TASKS.format(
            skill_plan_context=response_skill_plan, )

        response_skill_tasks = self._call_llm(
            user_prompt=prompt_skill_tasks,
            stream=self.stream,
        )
        skill_context.spec.tasks = response_skill_tasks
        logger.info('\n======== Completed Skill Tasks Response ========\n')

        # IMPLEMENTATION & EXECUTION
        script_contents: str = '\n\n'.join([
            '<!-- ' + script.get('path', '') + ' -->\n'
            + script.get('content', '') for script in skill_context.scripts
        ])
        reference_contents: str = '\n\n'.join([
            '<!-- ' + ref.get('path', '') + ' -->\n' + ref.get('content', '')
            for ref in skill_context.references
        ])
        resource_contents: str = '\n\n'.join([
            '<!-- ' + res.get('path', '') + ' -->\n' + res.get('content', '')
            for res in skill_context.resources
        ])

        prompt_tasks_implementation: str = PROMPT_TASKS_IMPLEMENTATION.format(
            script_contents=script_contents,
            reference_contents=reference_contents,
            resource_contents=resource_contents,
            skill_tasks_context=response_skill_tasks,
            scripts_implementation_format=SCRIPTS_IMPLEMENTATION_FORMAT,
        )

        response_tasks_implementation = self._call_llm(
            user_prompt=prompt_tasks_implementation,
            stream=self.stream,
        )
        skill_context.spec.implementation = response_tasks_implementation

        # Dump the spec files
        spec_output_path = skill_context.spec.dump(
            output_dir=str(self.work_dir))
        logger.info(f'Spec files dumped to: {spec_output_path}')

        # Extract IMPLEMENTATION content and determine execution scenario
        _, implementation_content = self._extract_implementation(
            content=response_tasks_implementation)

        if not implementation_content or len(implementation_content) == 0:
            logger.error('No IMPLEMENTATION content extracted from response')
            return 'I was unable to determine the implementation steps required to complete your request.'

        else:
            temp_item = implementation_content[0]
            if isinstance(temp_item, dict):
                execute_results: List[dict] = []
                for _code_block in implementation_content:
                    execute_result: Dict[str, Any] = self.execute(
                        code_block=_code_block,
                        skill_context=skill_context,
                    )
                    execute_results.append(execute_result)

                    if execute_result.get('success', False):
                        logger.info(f'Execution result: {execute_result}')
                    else:
                        logger.error(
                            f'Execution failed: {execute_result} for code block: {_code_block}'
                        )
                return json.dumps(
                    execute_results, ensure_ascii=False, indent=2)
            elif isinstance(temp_item, tuple):
                # Dump the generated code content to files
                for _lang, _code in implementation_content:
                    if _lang == 'html':
                        file_ext = 'html'
                    elif _lang == 'javascript':
                        file_ext = 'js'
                    else:
                        file_ext = 'md'

                    output_file_path = self.work_dir / f'{str_to_md5(_code)}.{file_ext}'
                    with open(output_file_path, 'w', encoding='utf-8') as f:
                        f.write(_code)
                    logger.info(
                        f'Generated {_lang} file saved to: {output_file_path}')
                return f'Generated files have been saved to the working directory: {self.work_dir}'
            elif isinstance(temp_item, str):
                return '\n\n'.join(implementation_content)
            else:
                logger.error('Unknown IMPLEMENTATION content format')
                return 'I encountered an unexpected format in the implementation steps.'

    @staticmethod
    def _find_skill_dir(root: Union[str, List[str]]) -> List[str]:
        """
        Find all skill directories containing SKILL.md

        Args:
            root: Root directory to search

        Returns:
            list: List of skill directory paths
        """
        if isinstance(root, str):
            root_paths = [Path(root).resolve()]
        else:
            root_paths = [Path(p).resolve() for p in root]

        folders = []

        for root_path in root_paths:
            if not root_path.exists():
                continue
            for item in root_path.rglob('SKILL.md'):
                if item.is_file():
                    folders.append(str(item.parent))

        return list(dict.fromkeys(folders))

    @staticmethod
    def _extract_implementation(content: str) -> Tuple[str, List[Any]]:
        """
        Extract IMPLEMENTATION content and determine execution scenario.
            e.g. <IMPLEMENTATION> ... </IMPLEMENTATION>

        Args:
            content: Full text containing IMPLEMENTATION tag

        Returns:
            Tuple of (scenario_type, results)
                scenario_type: 'script_execution', 'code_generation', or 'unable_to_execute'
                results: List of parsed results based on scenario
        """
        impl_content: str = extract_by_tag(text=content, tag='IMPLEMENTATION')
        results: List[Any] = []
        # Scenario 1: Script Execution
        try:
            results: List[Dict[str, Any]] = json.loads(impl_content)
        except Exception as e:
            logger.debug(f'Failed to parse IMPLEMENTATION as JSON: {str(e)}')

        if len(results) > 0:
            return 'script_execution', results

        # Scenario 2: No Script Execution, output JavaScript or HTML code blocks
        results: List[str] = re.findall(r'```(html|javascript)\s*\n(.*?)\n```',
                                        impl_content, re.DOTALL)
        if len(results) > 0:
            return 'code_generation', results

        # Scenario 3: Unable to Execute Any Script, provide reason (string)
        return 'unable_to_execute', [impl_content]

    @staticmethod
    def _extract_cmd_from_code_blocks(text) -> List[str]:
        """
        Extract ```shell ... ``` code block from text.

        Args:
            text (str): Text containing shell code blocks

        Returns:
            list: List of parsed str
        """
        pattern = r'```shell\s*\n(.*?)\n```'
        matches = re.findall(pattern, text, re.DOTALL)

        results = []
        for shell_str in matches:
            try:
                cleaned_shell_str = shell_str.strip()
                results.append(cleaned_shell_str)
            except Exception as e:
                raise RuntimeError(
                    f'Failed to decode shell command: {e}\nProblematic shell string: {shell_str}'
                )

        return results

    @staticmethod
    def _extract_packages_from_code_blocks(text) -> List[str]:
        """
        Extract ```packages ... ``` content from input text.

        Args:
            text (str): Text containing packages code blocks

        Returns:
            list: List of packages, e.g. ['numpy', 'torch', ...]
        """
        pattern = r'```packages\s*\n(.*?)\n```'
        matches = re.findall(pattern, text, re.DOTALL)

        results = []
        for packages_str in matches:
            try:
                cleaned_packages_str = packages_str.strip()
                results.append(cleaned_packages_str)
            except Exception as e:
                raise RuntimeError(
                    f'Failed to decode shell command: {e}\nProblematic shell string: {packages_str}'
                )

        results = '\n'.join(results).splitlines()
        return results

    def _analyse_code_block(self, code_block: dict,
                            skill_context: SkillContext) -> Dict[str, str]:
        """
        Analyse a code block from a skill context to extract executable command.

        Args:
            code_block: Code block dictionary containing 'script' or 'function' key
                e.g. {{'script': '<script_path>', 'parameters': {{'param1': 'value1', 'param2': 'value2', ...}}}}
            skill_context: SkillContext object

        Returns:
            Dictionary containing:
                'type': 'script' or 'function'
                'cmd': Executable command string
                'packages': List of required packages
        """
        # type - script or function
        res = {'type': '', 'cmd': '', 'packages': []}

        # Get the script path
        if 'script' in code_block:
            script_str: str = os.path.basename(code_block.get('script'))
            parameters: Dict[str, Any] = code_block.get('parameters', {})

            # Get real script absolute path
            script_path: Path = skill_context.skill.skill_path / script_str
            if not script_path.exists():
                script_path: Path = skill_context.skill.skill_path / 'scripts' / script_str
            if not script_path.exists():
                raise FileNotFoundError(f'Script not found: {script_str}')

            # Read the content of script
            try:
                with open(script_path, 'r', encoding='utf-8') as f:
                    script_content = f.read()

                script_content = script_content.strip()
                if not script_content:
                    raise RuntimeError(f'Script is empty: {script_str}')

                # Build command to execute the script with parameters
                prompt: str = (
                    f'According to following script content and parameters, '
                    f'find the usage for script and output the shell command in the form of: '
                    f'```shell\npython {script_path} ...\n``` with python interpreter. '
                    f'\nExtract the packages required by the script and output them in the form of: ```packages\npackage1\npackage2\n...```. '  # noqa
                    f'Note that you need to exclude the build-in standard library packages, and determine the specific PyPI package name according to the import statements in the script. '  # noqa
                    f'you must output the result very concisely and clearly without any extra explanation.'
                    f'\n\nSCRIPT CONTENT:\n{script_content}'
                    f'\n\nPARAMETERS:\n{json.dumps(parameters, ensure_ascii=False)}'
                )
                response: str = self._call_llm(
                    user_prompt=prompt,
                    system_prompt=
                    'You are a helpful assistant that extracts the shell command from code blocks.',
                    stream=self.stream,
                )

                cmd_blocks = self._extract_cmd_from_code_blocks(response)
                if len(cmd_blocks) == 0:
                    raise RuntimeError(
                        f'No shell command found in LLM response for script {script_str}'
                    )
                cmd_str = cmd_blocks[0]  # TODO: NOTE

                packages = self._extract_packages_from_code_blocks(response)

                res['type'] = 'script'
                res['cmd'] = cmd_str
                res['packages'] = packages

            except Exception as e:
                raise RuntimeError(
                    f'Failed to read script {script_str}: {str(e)}')

        elif 'function' in code_block:
            # TODO: to be implemented
            ...

        else:
            raise ValueError(
                "Code block must contain either 'script' or 'function' key")

        return res

    def execute(self, code_block: Dict[str, Any],
                skill_context: SkillContext) -> Dict[str, Any]:
        """
        Execute a code block from a skill context.

        Args:
            code_block: Code block dictionary containing 'script' or 'function' key
                e.g. {{'script': '<script_path>', 'parameters': {{'param1': 'value1', 'param2': 'value2', ...}}}}
            skill_context: SkillContext object

        Returns:
            Dictionary containing execution results
        """

        try:
            executable_code: Dict[str, str] = self._analyse_code_block(
                code_block=code_block,
                skill_context=skill_context,
            )
            code_type: str = executable_code.get('type')
            cmd_str: str = executable_code.get('cmd')
            packages: list = executable_code.get('packages', [])

            if not cmd_str:
                raise RuntimeError(
                    'No command to execute extracted from code block')
        except Exception as e:
            logger.error(f'Error analyzing code block: {str(e)}')
            return {
                'success': False,
                'error': str(e),
                'output': '',
                'return_code': -1
            }

        if 'script' == code_type:

            try:
                # Prepare execution environment
                logger.info(f'Installing required packages: {packages}')
                for pack in packages:
                    install_package(package_name=pack)

                execution_result = self._execute_python_script(cmd_str=cmd_str)

                logger.info(
                    f"Script execution completed with return code: {execution_result['return_code']}"
                )
                return execution_result

            except Exception as e:
                logger.error(f'Error executing script `{cmd_str}`: {str(e)}')
                return {
                    'success': False,
                    'error': str(e),
                    'output': '',
                    'return_code': -1
                }

        elif 'function' == code_type:
            # TODO: to be implemented
            ...
        else:
            return {
                'success': False,
                'error':
                "Code block must contain either 'script' or 'function' key.",
                'output': '',
                'return_code': -1
            }

    def _execute_python_script(self, cmd_str: str) -> Dict[str, Any]:

        try:
            return self._execute_as_subprocess(cmd_str=cmd_str)

        except Exception as e:
            return {
                'success': False,
                'error': f'Execution failed: {str(e)}',
                'output': '',
                'return_code': -1
            }

    def _execute_as_subprocess(self,
                               cmd_str: str,
                               timeout: int = 180) -> Dict[str, Any]:
        """
        Execute script as subprocess.

        Args:
            cmd_str: Command string to execute

        Returns:
            Execution result info
        """
        try:
            # Build command
            cmd: list = [sys.executable]
            cmd.extend(cmd_str.split(' ')[1:])

            # Execute subprocess
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.work_dir)

            return {
                'success': result.returncode == 0,
                'output': result.stdout,
                'error': result.stderr,
                'return_code': result.returncode
            }

        except subprocess.TimeoutExpired:
            return {
                'success': False,
                'error': 'Script execution timed out (30s)',
                'output': '',
                'return_code': -1
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'output': '',
                'return_code': -1
            }


def create_agent_skill(
    skills: Union[str, List[str], List[SkillSchema]],
    model: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    stream: Optional[bool] = True,
    work_dir: str = None,
) -> AgentSkill:
    """
    Create an AgentSkill instance.

    Args:
        skills: Path(s) to skill directories,
            the root path of skill directories, list of SkillSchema, or skill IDs on the hub
            Note: skill IDs on the hub are not yet implemented.
        api_key: OpenAI API key
        base_url: Custom API base URL
        model: LLM model name
        stream: Whether to stream responses
        work_dir: Working directory.

    Returns:
        AgentSkill instance.
    """
    return AgentSkill(
        skills=skills,
        api_key=api_key,
        base_url=base_url,
        model=model,
        stream=stream,
        work_dir=work_dir,
    )
