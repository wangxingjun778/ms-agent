# Copyright (c) ModelScope Contributors. All rights reserved.
from importlib import import_module

_EXPORTS = {
    'SkillCatalog': 'catalog',
    'SkillDescriptor': 'discovery',
    'SkillLoader': 'loader',
    'load_skills': 'loader',
    'SkillPromptInjector': 'prompt_injector',
    'SafetyFinding': 'safety',
    'SkillSafetyReport': 'safety',
    'SkillSafetyScanner': 'safety',
    'SkillFile': 'schema',
    'SkillSchema': 'schema',
    'SkillSchemaParser': 'schema',
    'SkillSearchEngine': 'search',
    'SkillToolSet': 'skill_tools',
    'SkillSource': 'sources',
    'SkillSourceType': 'sources',
    'parse_skill_source': 'sources',
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(f'.{_EXPORTS[name]}', __name__), name)
    globals()[name] = value
    return value


__all__ = [
    'SkillSchema',
    'SkillSchemaParser',
    'SkillFile',
    'SkillDescriptor',
    'SkillLoader',
    'load_skills',
    'SkillSource',
    'SkillSourceType',
    'parse_skill_source',
    'SkillCatalog',
    'SkillPromptInjector',
    'SkillToolSet',
    'SkillSearchEngine',
    'SkillSafetyScanner',
    'SkillSafetyReport',
    'SafetyFinding',
]
