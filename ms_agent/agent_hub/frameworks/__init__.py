# Copyright (c) ModelScope Contributors. All rights reserved.
"""Auto-register all built-in framework workspace specifications.

Importing this package triggers registration of all bundled frameworks into
:data:`ms_agent.agent_hub.FRAMEWORK_REGISTRY`.
"""

from . import (
    hermes,  # noqa: F401
    ms_agent,  # noqa: F401
    nanobot,  # noqa: F401
    openclaw,  # noqa: F401
    openhuman,  # noqa: F401
    qoder,  # noqa: F401
    qwenpaw,  # noqa: F401
)
