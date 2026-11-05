# Copyright (c) ModelScope Contributors. All rights reserved.
from importlib import import_module

__all__ = ['LLMAgent']


def __getattr__(name):
    if name == 'LLMAgent':
        value = import_module('.agent.llm_agent', __name__).LLMAgent
        globals()[name] = value
        return value
    raise AttributeError(name)
