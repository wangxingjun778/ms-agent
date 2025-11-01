from typing import Any, Dict, List, Optional, Union

from ms_agent.utils.utils import install_package, logger


class EnclaveSandbox:
    """
    A sandbox environment for securely executing code and commands based on `ms-enclave`.

    See `https://github.com/modelscope/ms-enclave`
    """

    def __init__(self, **kwargs):

        self._init()

        from ms_enclave.sandbox import SandboxConfig, Sandbox, DockerSandboxConfig

        self.sandbox_config: SandboxConfig = DockerSandboxConfig(
            image=kwargs.pop('image', None) or 'python:3.11-slim',
            memory_limit=kwargs.pop('memory_limit', None) or '512m',
            tools_config={
                'python_executor': kwargs.pop('python_executor', None) or {},
                'file_operation': kwargs.pop('file_operation', None) or {},
                'shell_executor': kwargs.pop('shell_executor', None) or {}
            })

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

    async def async_execute(
            self,
            python_code: Optional[str] = '',
            shell_command: Optional[Union[str, List[str]]] = None,
            requirements: Optional[List[str]] = None) -> Dict[str, Any]:
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
                requirements_file = '/sandbox/requirements.txt'
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
                result_python_code = await sandbox.execute_tool(
                    'python_executor', {'code': python_code})

                results['python_executor'].append({
                    'output':
                    result_python_code.output,
                    'error':
                    result_python_code.error,
                    'status':
                    result_python_code.status
                })

            if shell_command:
                if isinstance(shell_command, list):
                    shell_command = ' '.join(shell_command)
                result_shell_command = await sandbox.execute_command(
                    shell_command)

                results['shell_executor'].append({
                    'output':
                    result_shell_command.stdout,
                    'error':
                    result_shell_command.stderr,
                    'status':
                    result_shell_command.status
                })

        return results
