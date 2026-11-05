# Copyright (c) ModelScope Contributors. All rights reserved.
from importlib import import_module

from .llm_utils import async_retry, is_retryable_error, retry
from .logger import get_logger

_EXPORTS = {
    'get_fact_retrieval_prompt': 'prompt',
    'assert_package_exist': 'utils',
    'enhance_error': 'utils',
    'read_history': 'utils',
    'save_history': 'utils',
    'strtobool': 'utils',
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(f'.{_EXPORTS[name]}', __name__), name)
    globals()[name] = value
    return value


__all__ = ['async_retry', 'is_retryable_error', 'retry', 'get_logger', 'MAX_CONTINUE_RUNS', *_EXPORTS]

MAX_CONTINUE_RUNS = 3
