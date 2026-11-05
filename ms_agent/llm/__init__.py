# Copyright (c) ModelScope Contributors. All rights reserved.
# Data-driven provider layer (opt-in via config.llm.use_provider_router).
from importlib import import_module

_EXPORTS = {
    'ResponseAdapter': 'adapter',
    'CredentialResolver': 'credentials',
    'LLM': 'llm',
    'LLMProvider': 'router',
    'ProviderRouter': 'router',
    'ProviderRegistry': 'spec',
    'ProviderSpec': 'spec',
    'get_registry': 'spec',
    'LLMResponse': 'types',
    'ProviderCapabilities': 'types',
    'ProviderCapability': 'types',
    'TextBlock': 'types',
    'ThinkingBlock': 'types',
    'ToolUseBlock': 'types',
    'UsageInfo': 'types',
    'Message': 'utils',
    'collect_response': 'utils',
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(f'.{_EXPORTS[name]}', __name__), name)
    globals()[name] = value
    return value


__all__ = [
    'LLM',
    'Message',
    'collect_response',
    'ProviderRouter',
    'LLMProvider',
    'ProviderRegistry',
    'ProviderSpec',
    'get_registry',
    'CredentialResolver',
    'ResponseAdapter',
    'LLMResponse',
    'UsageInfo',
    'TextBlock',
    'ToolUseBlock',
    'ThinkingBlock',
    'ProviderCapability',
    'ProviderCapabilities',
]
