"""Agent service — top-level entry point for the lightweight agent system.

:class:`AgentService` is the public API surface.  Given a resolved
configuration dict it constructs the per-request
:class:`~ms_agent.agent.context.RunContext` (user-isolated session store,
memory service, skill facade, tool registry) and delegates the actual
function-calling loop to :class:`~ms_agent.agent.runner.AgentRunner`.

Typical usage::

    async with AgentService(config) as svc:
        async for event in svc.run(request):
            print(event.to_dict())

Or for non-streaming::

    async with AgentService(config) as svc:
        response = await svc.run_sync(request)
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from copy import deepcopy
from typing import Any, AsyncIterator, Dict, List, Optional
from uuid import uuid4

from ms_agent.agent.context import RunContext, SkillService
from ms_agent.agent.runner import AgentRunner
from ms_agent.agent.schema import (
    AgentRequest,
    AgentResponse,
    MessageInput,
    StreamEvent,
)
from ms_agent.llm.http_client import HTTPLLMClient
from ms_agent.memory.service import MemoryService
from ms_agent.session.locking import PathLocks
from ms_agent.session.store import HistoryCache, SessionStore
from ms_agent.tools.api_registry import APIToolRegistry
from ms_agent.utils.limiter import UpstreamLimiter

logger = logging.getLogger(__name__)

__all__ = ['AgentService']


class AgentService:
    """Top-level entry point for the lightweight agent system.

    Assembles a :class:`RunContext` from an :class:`AgentRequest` and
    delegates execution to a stateless :class:`AgentRunner`.

    Args:
        config: Resolved agent configuration dict.  Expected top-level keys:

            - ``agent.system_prompt`` — Base system prompt.
            - ``agent.max_chat_round`` — Max function-calling rounds.
            - ``tools.api_tools_config`` — Path to ``api_tools.yaml``.
            - ``memory.base_dir`` — Memory storage base directory.
            - ``memory.enabled`` — Whether memory is enabled.
            - ``session.base_dir`` — Session storage base directory.
            - ``llm.*`` — LLM configuration (model, api_key, base_url, …).
            - ``skills.dirs`` — List of skill directories (optional).
    """

    def __init__(self, config: Dict[str, Any], *, locks: Optional[PathLocks] = None, llm_client: Any = None) -> None:
        self._config = deepcopy(config)
        self._locks = locks or PathLocks()
        self._llm_client = llm_client
        self._http_session = None
        self._skill_service = None
        self._runner = AgentRunner()
        self._tool_registry: Optional[APIToolRegistry] = None
        self._initialized = False
        self._sessions: list = []
        self.limiters: dict = {}
        self._session_stores: OrderedDict[str, SessionStore] = OrderedDict()
        self._history_cache = HistoryCache()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize shared resources (tool registry, …).  Call once at startup."""
        if self._initialized:
            return

        tools_config = self._config.get('tools', {})
        api_config_path = str(tools_config.get('api_tools_config', '') or '')

        import aiohttp

        upstreams = {'llm': self._config.get('llm', {}).get('upstream', {}), **tools_config.get('upstreams', {})}
        sessions = {}
        for name in ('llm', 'submit', 'poll'):
            options = upstreams.get(name, {})
            sessions[name] = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    limit=options.get('connections', 100), limit_per_host=options.get('connections', 100)
                )
            )
            self._sessions.append(sessions[name])
            self.limiters[name] = UpstreamLimiter(options.get('concurrency', 32), options.get('rpm', 0))
        self._http_session = sessions['llm']
        self._tool_registry = APIToolRegistry(
            sessions['submit'],
            strict=bool(self._config.get('strict')),
            poll_client=sessions['poll'],
            limiters=self.limiters,
            upstreams=upstreams,
        )
        if tools_config.get('api'):
            try:
                await self._tool_registry.load_from_dict(tools_config['api'])
            except BaseException:
                await self.shutdown()
                raise
        elif api_config_path:
            try:
                await self._tool_registry.load_from_config(api_config_path)
            except FileNotFoundError:
                if self._config.get('strict'):
                    await self.shutdown()
                    raise
                logger.warning(
                    'API tools config not found at %s; continuing without API tools',
                    api_config_path,
                )
            except Exception:  # noqa: BLE001
                if self._config.get('strict'):
                    await self.shutdown()
                    raise
                logger.warning(
                    'Failed to load API tools from %s',
                    api_config_path,
                    exc_info=True,
                )

        skills_config = self._config.get('skills', {})
        if skills_config.get('enabled', True) and skills_config.get('dirs'):
            self._skill_service = SkillService(
                list(skills_config['dirs']),
                {**skills_config, 'strict': bool(self._config.get('strict'))},
            )
            try:
                self._skill_service.build_skill_prompt_section()
            except BaseException:
                await self.shutdown()
                raise
        self._initialized = True

    async def shutdown(self) -> None:
        """Graceful shutdown.  Close shared resources."""
        if self._tool_registry is not None:
            await self._tool_registry.close()
        for session in self._sessions:
            await session.close()
        self._sessions.clear()
        self._http_session = None
        self._session_stores.clear()
        self._history_cache.clear()
        self._initialized = False

    async def __aenter__(self) -> 'AgentService':
        await self.initialize()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.shutdown()

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    async def run(
        self,
        request: AgentRequest,
        *,
        run_uuid: str = '',
        control: Any = None,
        record: Any = None,
    ) -> AsyncIterator[StreamEvent]:
        """Process a request, yielding :class:`StreamEvent` objects.

        Constructs per-request, user-isolated services and delegates to
        :class:`AgentRunner`.
        """
        if not self._initialized:
            await self.initialize()

        # Resolve session_id
        session_id = request.session_id or str(uuid4())

        # Per-user session store
        session_base = str(
            self._config.get('session', {}).get(
                'base_dir',
                '/tmp/ms_agent/sessions',
            ),
        )
        session_store = self._session_stores.get(request.user_id)
        if session_store is None:
            session_store = SessionStore(
                session_base,
                request.user_id,
                self._locks,
                strict=bool(self._config.get('strict')),
                optimized=bool(self._config.get('session', {}).get('optimized')),
                cache=self._history_cache,
                cache_entries=self._config.get('session', {}).get('cache_entries', 256),
                cache_bytes=self._config.get('session', {}).get('cache_bytes', 67108864),
            )
            self._session_stores[request.user_id] = session_store
            if len(self._session_stores) > self._config.get('session', {}).get('cache_entries', 256):
                self._session_stores.popitem(last=False)
        else:
            self._session_stores.move_to_end(request.user_id)
        await session_store.create_session(session_id)

        # Per-user memory service
        memory_base = str(
            self._config.get('memory', {}).get(
                'base_dir',
                '/tmp/ms_agent/memory',
            ),
        )
        memory_config = self._config.get('memory', {})
        memory_service = MemoryService(
            memory_base,
            request.user_id,
            session_id=session_id if memory_config.get('scope') == 'session' else None,
            locks=self._locks,
            strict=bool(self._config.get('strict')),
            projection=memory_config.get('projection', True),
        )

        # Skill service (optional)
        skill_service: Optional[SkillService] = None
        skill_service = self._skill_service

        # Convert MessageInput objects to internal message dicts
        messages = self._convert_messages(request.messages)

        # Assemble RunContext
        agent_config = self._config.get('agent', {})
        ctx = RunContext(
            user_id=request.user_id,
            session_id=session_id,
            messages=messages,
            config=deepcopy(agent_config),
            session_store=session_store,
            memory_service=memory_service,
            tool_registry=self._tool_registry,  # type: ignore[arg-type]
            skill_service=skill_service,
            llm_config=deepcopy(self._config.get('llm', {})),
            run_uuid=run_uuid,
            control=control,
            record=record,
            memory_enabled=bool(memory_config.get('enabled', True)),
            max_rounds=int(
                agent_config.get('max_chat_round', 20) or 20,
            ),
        )

        # Apply per-request config overrides
        if request.config_overrides:
            self._apply_overrides(ctx, request.config_overrides)

        if ctx.llm_config.get('transport') == 'http':
            ctx.llm_client = self._llm_client or HTTPLLMClient(
                ctx.llm_config, self._http_session, limiter=self.limiters['llm']
            )

        # Delegate to runner
        try:
            async for event in self._runner.run(ctx):
                yield event
        finally:
            # Close per-request resources
            await memory_service.close()

    async def run_sync(self, request: AgentRequest) -> AgentResponse:
        """Convenience: collect all events and return an :class:`AgentResponse`."""
        events: List[StreamEvent] = []
        async for event in self.run(request):
            events.append(event)
        session_id = request.session_id or (events[0].data.get('session_id', '') if events else '')
        return AgentResponse.from_events(session_id, events)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_messages(inputs: List[MessageInput]) -> List[Dict[str, Any]]:
        """Convert :class:`MessageInput` objects to internal message dicts."""
        messages: List[Dict[str, Any]] = []
        for msg in inputs:
            d: Dict[str, Any] = {
                'role': msg.role,
                'content': msg.content,
            }
            if msg.attachments:
                urls = [a.url for a in msg.attachments if a.url]
                if urls:
                    d['content'] += '\n参考图片（保持顺序）：\n' + '\n'.join(urls)
                d['attachments'] = [
                    {
                        'type': a.type,
                        'url': a.url,
                        'base64': a.base64,
                        'filename': a.filename,
                    }
                    for a in msg.attachments
                ]
            messages.append(d)
        return messages

    @staticmethod
    def _apply_overrides(ctx: RunContext, overrides: Dict[str, Any]) -> None:
        """Apply per-request configuration overrides to the context."""
        if 'max_rounds' in overrides:
            ctx.max_rounds = int(overrides['max_rounds'])
        if 'max_chat_round' in overrides:
            ctx.max_rounds = int(overrides['max_chat_round'])
        if 'system_prompt' in overrides:
            ctx.config = {**ctx.config, 'system_prompt': overrides['system_prompt']}
        if 'llm' in overrides and isinstance(overrides['llm'], dict):
            ctx.llm_config = {**ctx.llm_config, **overrides['llm']}
