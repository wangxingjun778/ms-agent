"""Run context for the stateless agent executor.

RunContext is the complete execution context that flows through every agent run.
It replaces per-instance mutable state with an explicitly threaded value object,
ensuring the runner itself remains stateless across requests.

SkillService provides a lightweight facade over the existing skill catalog and
prompt injector, falling back gracefully when the full skill infrastructure is
not available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ms_agent.memory.service import MemoryService
    from ms_agent.session.store import SessionStore
    from ms_agent.tools.api_registry import APIToolRegistry

logger = logging.getLogger(__name__)

__all__ = ['RunContext', 'SkillService']


# ---------------------------------------------------------------------------
# Skill service facade
# ---------------------------------------------------------------------------


class SkillService:
    """Lightweight skill service for prompt injection.

    Wraps :class:`~ms_agent.skill.catalog.SkillCatalog` and
    :class:`~ms_agent.skill.prompt_injector.SkillPromptInjector` when they are
    available.  If the skill infrastructure is missing (e.g. the ``skill``
    extra is not installed), all methods degrade to no-ops and return empty
    values so the runner never hard-fails on an optional feature.
    """

    def __init__(
        self,
        skill_dirs: Optional[List[str]] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._skill_dirs = skill_dirs or []
        self._config = config or {}
        self._catalog: Any = None
        self._injector: Any = None
        self._initialized = False

    # ---- lazy initialization ------------------------------------------------

    def _ensure_initialized(self) -> None:
        """Import and configure the catalog + injector once."""
        if self._initialized:
            return
        self._initialized = True
        try:
            from ms_agent.skill.catalog import SkillCatalog
            from ms_agent.skill.prompt_injector import SkillPromptInjector

            catalog = SkillCatalog()
            for skill_dir in self._skill_dirs:
                try:
                    catalog.scan_directory(skill_dir)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        'SkillService: failed to scan directory %s',
                        skill_dir,
                        exc_info=True,
                    )

            self._catalog = catalog
            injection_mode = self._config.get('prompt_injection', 'all')
            self._injector = SkillPromptInjector(
                catalog,
                prompt_injection=injection_mode,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                'SkillService: skill infrastructure not available, degrading gracefully',
                exc_info=True,
            )

    # ---- public API ---------------------------------------------------------

    def build_skill_prompt_section(self) -> str:
        """Build the skills section for the system prompt.

        Returns an empty string when no skills are configured or the skill
        infrastructure is unavailable.
        """
        self._ensure_initialized()
        if self._injector is None:
            return ''
        try:
            return self._injector.build_skill_prompt_section()
        except Exception:  # noqa: BLE001
            logger.warning(
                'SkillService: build_skill_prompt_section failed',
                exc_info=True,
            )
            return ''

    def get_active_skills(self) -> List[Dict[str, Any]]:
        """Return a list of active skill metadata dicts.

        Each dict contains at minimum ``id``, ``name``, and ``description``
        keys.  Returns an empty list when skills are unavailable.
        """
        self._ensure_initialized()
        if self._catalog is None:
            return []
        try:
            skills = self._catalog.get_enabled_skills()
            return [
                {
                    'id': sid,
                    'name': getattr(skill, 'name', sid),
                    'description': getattr(skill, 'description', ''),
                }
                for sid, skill in skills.items()
            ]
        except Exception:  # noqa: BLE001
            logger.warning(
                'SkillService: get_active_skills failed',
                exc_info=True,
            )
            return []


# ---------------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------------


@dataclass
class RunContext:
    """Complete execution context passed to every agent run.

    All mutable per-request state lives here — the :class:`AgentRunner`
    that receives it is entirely stateless.

    Attributes:
        user_id: Authenticated user identifier.
        session_id: Conversation session identifier.
        messages: Current request messages in internal dict format.
        config: Resolved agent configuration (read-only).
        session_store: Session persistence backend (user-isolated).
        memory_service: Memory retrieval / ingestion (user-isolated).
        tool_registry: Registered API tools.
        skill_service: Optional skill prompt injection service.
        llm_config: LLM configuration (model, api_key, base_url, …).
        max_rounds: Maximum function-calling rounds before forced stop.
        metadata: Extension fields (token counters, trace ids, …).
    """

    user_id: str
    session_id: str
    messages: List[Dict[str, Any]]
    config: Dict[str, Any]
    session_store: 'SessionStore'
    memory_service: 'MemoryService'
    tool_registry: 'APIToolRegistry'
    skill_service: Optional[SkillService] = None
    llm_config: Dict[str, Any] = field(default_factory=dict)
    max_rounds: int = 20
    metadata: Dict[str, Any] = field(default_factory=dict)
