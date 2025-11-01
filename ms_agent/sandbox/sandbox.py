import uuid
from typing import Any, Dict, List, Optional, Union

from ms_agent.utils.utils import install_package, logger


class EnclaveSandbox:
    """
    A sandbox environment for securely executing code and commands based on `ms-enclave`.

    See `https://github.com/modelscope/ms-enclave`
    """

    def __init__(self, **kwargs):
        """
        Initialize the EnclaveSandbox with optional configuration parameters.

        Args:
            **kwargs: Optional configuration parameters for the sandbox.
                - image (str): Docker image to use for the sandbox. Default is 'python:3.11-slim'.
                - memory_limit (str): Memory limit for the sandbox container. Default is '512m'.
                - python_executor (dict): Configuration for the Python executor tool.
                - file_operation (dict): Configuration for the file operation tool.
                - shell_executor (dict): Configuration for the shell executor tool.
                - volumes (list): List of tuples specifying host and container directory mounts in the form
                                  [(host_path, container_path, mode), ...] where mode is 'ro' or 'rw'.
        """
        self._init()

        from ms_enclave.sandbox import SandboxConfig, DockerSandboxConfig

        # Mount host directories into the sandbox container if provided
        _volumes = kwargs.pop('volumes', None) or []
        self.volume_dict: Dict[str, Dict[str, str]] = {}
        if _volumes:
            for host_path, container_path, mode in _volumes:
                self.volume_dict[host_path] = {
                    'bind': container_path,
                    'mode': mode
                }

        self.sandbox_config: SandboxConfig = DockerSandboxConfig(
            image=kwargs.pop('image', None) or 'python:3.11-slim',
            memory_limit=kwargs.pop('memory_limit', None) or '512m',
            tools_config={
                'python_executor': kwargs.pop('python_executor', None) or {},
                'file_operation': kwargs.pop('file_operation', None) or {},
                'shell_executor': kwargs.pop('shell_executor', None) or {}
            },
            volumes=self.volume_dict,
        )

    @staticmethod
    def _init():
        """
        Initialize the sandbox environment by ensuring the `ms-enclave` package is installed.

        Raises:
            Exception: If the installation of `ms-enclave` fails.
        """
        logger.info('Installing ms-enclave package...')
        try:
            install_package(
                package_name='ms-enclave', import_name='ms_enclave')
        except Exception as e:
            raise e

    async def async_execute(self,
                            python_code: Union[str, List[str]] = None,
                            shell_command: Union[str, List[str]] = None,
                            requirements: List[str] = None) -> Dict[str, Any]:
        """
        Asynchronously execute Python code and shell commands within the sandbox.

        Args:
            python_code (Optional[Union[str, List[str]]]): Python code snippet(s) to execute.
                e.g. "print('Hello World')", or ["print('Hello World')", "x = 5; print(x)"]
            shell_command (Optional[Union[str, List[str]]]): Shell command(s) to execute.
                e.g. "ls -al /data", or ["ls -al /data", "echo 'Hello World'"]
            requirements (Optional[List[str]]): List of Python packages to install before execution.

        Returns:
            Dict[str, Any]: A dictionary containing the results of the executions.
        """
        from ms_enclave.sandbox import SandboxFactory
        from ms_enclave.sandbox.model import SandboxType

        results: Dict[str, Any] = {
            'python_executor': [],
            'shell_executor': [],
            'file_operation': []
        }

        async with SandboxFactory.create_sandbox(
                SandboxType.DOCKER, self.sandbox_config) as sandbox:

            if requirements is not None and len(requirements) > 0:
                requirements_file = f'/{str(uuid.uuid4())}/requirements.txt'
                await sandbox.execute_tool(
                    'file_operation', {
                        'operation': 'write',
                        'file_path': f'{requirements_file}',
                        'content': '\n'.join(requirements)
                    })

                result_requirements = await sandbox.execute_command(
                    f'pip install -r {requirements_file}')
                logger.info(result_requirements.stdout)

            if python_code:
                if isinstance(python_code, str):
                    python_code = [python_code]

                for py_item in python_code:
                    py_result = await sandbox.execute_tool(
                        'python_executor', {'code': py_item})

                    results['python_executor'].append({
                        'output':
                        py_result.output,
                        'error':
                        py_result.error,
                        'status':
                        py_result.status
                    })

            if shell_command:
                if isinstance(shell_command, str):
                    shell_command = [shell_command]

                for shell_item in shell_command:
                    shell_result = await sandbox.execute_command(shell_item)

                    results['shell_executor'].append({
                        'output':
                        shell_result.stdout,
                        'error':
                        shell_result.stderr,
                        'status':
                        shell_result.status
                    })

        return results

    def execute(self,
                python_code: Union[str, List[str]] = None,
                shell_command: Union[str, List[str]] = None,
                requirements: List[str] = None) -> Dict[str, Any]:
        """
        Synchronously execute Python code and shell commands within the sandbox.

        Args:
            python_code (Optional[Union[str, List[str]]]): Python code snippet(s) to execute.
                e.g. "print('Hello World')", or ["print('Hello World')", "x = 5; print(x)"]
            shell_command (Optional[Union[str, List[str]]]): Shell command(s) to execute.
                e.g. "ls -al /data", or ["ls -al /data", "echo 'Hello World'"]
            requirements (Optional[List[str]]): List of Python packages to install before execution.

        Returns:
            Dict[str, Any]: A dictionary containing the results of the executions.
        """
        import asyncio

        return asyncio.run(
            self.async_execute(
                python_code=python_code,
                shell_command=shell_command,
                requirements=requirements))
