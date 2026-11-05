# Copyright (c) ModelScope Contributors. All rights reserved.
from importlib import import_module

_EXPORTS = {'Memory': 'base', 'DefaultMemory': 'utils', 'get_memory_meta_safe': 'utils', 'memory_mapping': 'utils'}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(f'.{_EXPORTS[name]}', __name__), name)
    globals()[name] = value
    return value
