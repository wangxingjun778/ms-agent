from importlib import import_module

from .session_log import SessionLog


def __getattr__(name):
    if name not in {'ContextAssembler', 'ViewStrategy'}:
        raise AttributeError(name)
    value = getattr(import_module('.context_assembler', __name__), name)
    globals()[name] = value
    return value


__all__ = [
    'SessionLog',
    'ContextAssembler',
    'ViewStrategy',
]
