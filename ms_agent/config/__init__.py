# Copyright (c) ModelScope Contributors. All rights reserved.
from importlib import import_module

_EXPORTS = {
    'Config': 'config',
    'Env': 'env',
    'MCPConfigManager': 'mcp_manager',
    'ResolvedMCPConfig': 'mcp_schema',
    'normalize_mcp_server_entry': 'mcp_schema',
    'ConfigResolver': 'resolver',
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(f'.{_EXPORTS[name]}', __name__), name)
    globals()[name] = value
    return value
