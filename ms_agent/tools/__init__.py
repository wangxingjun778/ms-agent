# Copyright (c) ModelScope Contributors. All rights reserved.
from importlib import import_module

_EXPORTS = {
    'AgentTool': 'agent_tool',
    'CodeExecutionTool': 'code',
    'SandboxManagerFactory': 'code',
    'LSPCodeServer': 'code_server',
    'FileSystemTool': 'filesystem_tool',
    'MCPClient': 'mcp_client',
    'TaskControlTool': 'task_control_tool',
    'TodoListTool': 'todolist_tool',
    'ToolManager': 'tool_manager',
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    try:
        value = getattr(import_module(f'.{_EXPORTS[name]}', __name__), name)
    except ImportError:
        if name != 'MCPClient':
            raise
        value = None
    globals()[name] = value
    return value
