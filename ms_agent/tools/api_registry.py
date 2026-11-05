"""API Tool Registry — loads and manages API-based tools.

The registry is the lifecycle-owning counterpart to
:class:`ms_agent.tools.api_adapter.APIToolAdapter`. It parses the YAML
config, instantiates one adapter per declared tool, owns the shared
aiohttp client (unless one is injected), and dispatches tool calls by
name. Agent-runtime code should see only this class — the adapter stays
hidden behind :meth:`execute` so a future transport change (e.g. a
batching proxy) is a drop-in replacement.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from jsonschema import Draft202012Validator

from ms_agent.tools.schema import ToolInvocationContext

try:
    import aiohttp  # type: ignore

    _HAS_AIOHTTP = True
except ImportError:  # pragma: no cover — aiohttp is declared in requirements
    aiohttp = None  # type: ignore
    _HAS_AIOHTTP = False

try:
    import yaml  # type: ignore

    _HAS_YAML = True
except ImportError:  # pragma: no cover — PyYAML is declared in requirements
    yaml = None  # type: ignore
    _HAS_YAML = False

from ms_agent.tools.api_adapter import (
    APIConfig,
    APIToolAdapter,
    APIToolSpec,
    AuthConfig,
    OutputMapping,
    ToolResult,
)

logger = logging.getLogger(__name__)

__all__ = ['APIToolRegistry']

# ``${VAR}`` or ``${VAR:default}`` — matches the Bash-ish default syntax used
# in the sample YAML. The default value is captured verbatim; we don't try to
# interpret quoting because YAML already handles that one level up.
_ENV_PATTERN = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}')


def _resolve_env(value: Any) -> Any:
    """Recursively replace ``${VAR}`` / ``${VAR:default}`` tokens in strings.

    Non-string leaves pass through untouched; this keeps numeric fields
    (timeout, poll_interval) typed correctly after a round-trip through
    YAML.
    """
    if isinstance(value, str):

        def _sub(match: 're.Match[str]') -> str:
            name = match.group(1)
            default = match.group(2) or ''
            return os.environ.get(name, default)

        return _ENV_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(item) for item in value]
    return value


def _auth_from_dict(data: Optional[Dict[str, Any]]) -> AuthConfig:
    """Build an :class:`AuthConfig` from a parsed YAML fragment."""
    if not data:
        return AuthConfig(type='none')
    return AuthConfig(
        type=str(data.get('type', 'none')),
        token=data.get('token'),
        token_env=data.get('token_env'),
        header_name=str(data.get('header_name', 'Authorization')),
        header_prefix=str(data.get('header_prefix', 'Bearer')),
    )


def _api_from_dict(data: Dict[str, Any]) -> APIConfig:
    return APIConfig(
        method=str(data.get('method', 'POST')).upper(),
        path=str(data.get('path', '')),
        async_mode=bool(data.get('async_mode', False)),
        poll_path=data.get('poll_path'),
        poll_interval=float(data.get('poll_interval', 5.0)),
        poll_timeout=float(data.get('poll_timeout', 600.0)),
        headers=dict(data.get('headers') or {}),
        timeout=float(data.get('timeout', 120.0)),
    )


def _output_from_dict(data: Optional[Dict[str, Any]]) -> OutputMapping:
    data = data or {}
    kwargs: Dict[str, Any] = {}
    for key in ('success_path', 'error_path', 'task_id_path', 'status_path'):
        if key in data and data[key] is not None:
            kwargs[key] = str(data[key])
    if 'success_statuses' in data and data['success_statuses']:
        kwargs['success_statuses'] = [str(s) for s in data['success_statuses']]
    if 'failure_statuses' in data and data['failure_statuses']:
        kwargs['failure_statuses'] = [str(s) for s in data['failure_statuses']]
    return OutputMapping(**kwargs)


def _spec_from_dict(entry: Dict[str, Any]) -> APIToolSpec:
    """Validate and build an :class:`APIToolSpec` from one YAML tool entry."""
    if 'name' not in entry or 'description' not in entry:
        raise ValueError(f'Tool entry missing required name/description: {entry!r}')
    if 'parameters' not in entry:
        raise ValueError(f'Tool {entry.get("name")!r} missing parameters JSON Schema')
    return APIToolSpec(
        name=str(entry['name']),
        description=str(entry['description']),
        api_config=_api_from_dict(entry.get('api') or {}),
        parameters=dict(entry['parameters']),
        output_mapping=_output_from_dict(entry.get('output_mapping')),
        auth=_auth_from_dict(entry.get('auth')) if entry.get('auth') else None,
        requires_approval=bool(entry.get('requires_approval', False)),
    )


class APIToolRegistry:
    """Registry for API-based tools.

    Loads tool declarations from a YAML config (or an in-memory dict),
    exposes OpenAI-format tool specs for the LLM runtime, and dispatches
    calls by name. The HTTP client lifecycle is managed here so each tool
    adapter does not have to open its own connection pool: pass an
    ``http_client`` to piggy-back on an existing session, or let the
    registry create and close one via its ``async with`` interface.
    """

    def __init__(
        self,
        http_client: Optional['aiohttp.ClientSession'] = None,
        *,
        strict: bool = False,
        credential_provider: Optional[Callable[[ToolInvocationContext], Awaitable[Dict[str, str]]]] = None,
    ) -> None:
        self._tools: Dict[str, APIToolAdapter] = {}
        self._http_client = http_client
        self._owns_client = False
        self._loaded = False
        self._strict = strict
        self._credential_provider = credential_provider

    # ---- Loading ------------------------------------------------------------

    async def load_from_config(self, config_path: str) -> None:
        """Load tools from a YAML config file (``api_tools.yaml`` format)."""
        if not _HAS_YAML:
            raise RuntimeError("PyYAML is required for load_from_config; install via 'pip install pyyaml'")
        if not config_path or not os.path.isfile(config_path):
            raise FileNotFoundError(f'API tools config not found: {config_path}')
        with open(config_path, 'r', encoding='utf-8') as fh:
            raw = yaml.safe_load(fh) or {}
        await self.load_from_dict(raw)

    async def load_from_dict(self, config: Dict[str, Any]) -> None:
        """Load tools from an already-parsed config dict.

        Env-var tokens in the YAML (``${API_BASE_URL:http://...}``) are
        resolved here, not at YAML-parse time, so the same loaded file can
        be fed through this method twice with different environments
        between calls (useful for tests).
        """
        resolved = _resolve_env(config)
        root = resolved.get('api_tools') or resolved
        base_url = str(root.get('base_url', '')).strip()
        if not base_url:
            raise ValueError('api_tools.base_url is required')
        default_auth = _auth_from_dict(root.get('auth'))

        await self._ensure_client()

        tools = root.get('tools') or []
        if not isinstance(tools, list):
            raise ValueError('api_tools.tools must be a list')

        count = 0
        for entry in tools:
            if not isinstance(entry, dict):
                logger.warning('Skipping non-mapping tool entry: %r', entry)
                continue
            try:
                spec = _spec_from_dict(entry)
            except ValueError as exc:
                if self._strict:
                    raise
                logger.error('Invalid API tool config: %s', exc)
                continue
            if not entry.get('enabled', True):
                continue
            Draft202012Validator.check_schema(spec.parameters)
            if (
                self._strict
                and (spec.auth or default_auth).type != 'none'
                and not (spec.auth or default_auth).resolve_token()
            ):
                raise ValueError(f'工具 {spec.name} 缺少凭据')
            self.register(spec, base_url, default_auth)
            count += 1

        self._loaded = True
        logger.info('APIToolRegistry: loaded %d tools from config', count)

    def register(
        self,
        spec: APIToolSpec,
        base_url: str,
        default_auth: AuthConfig,
    ) -> None:
        """Register a single tool programmatically.

        Replaces any previously registered tool with the same name; a
        warning is emitted so an accidental name clash in a hand-edited
        YAML is at least visible in logs.
        """
        if spec.name in self._tools and self._strict:
            raise ValueError(f'工具名称重复: {spec.name}')
        if spec.name in self._tools:
            logger.warning('APIToolRegistry: replacing existing tool %r', spec.name)
        adapter = APIToolAdapter(
            spec=spec,
            base_url=base_url,
            default_auth=default_auth,
            http_client=self._http_client,
        )
        self._tools[spec.name] = adapter

    def unregister(self, tool_name: str) -> None:
        """Remove a tool from the registry; no-op if it isn't registered."""
        self._tools.pop(tool_name, None)

    # ---- Introspection ------------------------------------------------------

    def get_tool_specs(self) -> List[Dict[str, Any]]:
        """Return every tool's OpenAI function-calling spec.

        The list is sorted by name so the prompt prefix is stable across
        runs — matches the ordering strategy in
        :meth:`ms_agent.tools.tool_manager.ToolManager.get_tools`.
        """
        return [tool.get_tool_spec() for tool in sorted(self._tools.values(), key=lambda t: t.name)]

    def get_tool_names(self) -> List[str]:
        return sorted(self._tools)

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    # ---- Execution ----------------------------------------------------------

    def parameters(self, tool_name: str) -> Dict[str, Any]:
        return self._tools[tool_name].parameters_schema

    def requires_approval(self, tool_name: str) -> bool:
        return self._tools[tool_name]._spec.requires_approval

    async def execute(
        self,
        tool_name: str,
        args: Dict[str, Any],
        context: Optional[ToolInvocationContext] = None,
    ) -> ToolResult:
        """Execute a tool by name.

        Raises
        ------
        KeyError
            If ``tool_name`` is not registered. The agent runtime maps
            this to the same ``Tool "<name>" is not available`` message
            that :class:`ToolManager` produces for unknown MCP tools.
        """
        if tool_name not in self._tools:
            raise KeyError(tool_name)
        Draft202012Validator(self.parameters(tool_name)).validate(args)
        if context is not None and self._credential_provider is not None:
            from dataclasses import replace

            context = replace(context, credentials=await self._credential_provider(context))
        return await self._tools[tool_name].execute(args or {}, context)

    # ---- Lifecycle ----------------------------------------------------------

    async def _ensure_client(self) -> None:
        """Create a shared aiohttp session if none was injected."""
        if self._http_client is not None:
            return
        if not _HAS_AIOHTTP:
            # Adapters will fall back to urllib-on-thread; log once so this
            # is visible when the fallback path is exercised in production.
            logger.warning('aiohttp not available; API tools will use blocking urllib in a worker thread')
            return
        self._http_client = aiohttp.ClientSession()
        self._owns_client = True
        # Push the new client into any adapters that were registered before
        # loading finished (none in the happy path, but keep the invariant).
        for adapter in self._tools.values():
            adapter._client = self._http_client  # noqa: SLF001

    async def close(self) -> None:
        """Close the owned HTTP client; idempotent."""
        if self._owns_client and self._http_client is not None:
            try:
                await self._http_client.close()
            except Exception:  # noqa: BLE001 — cleanup must not raise
                logger.exception('Failed to close aiohttp client cleanly')
            finally:
                self._http_client = None
                self._owns_client = False
                # Give aiohttp a tick to actually release the TCP transport —
                # without this the loop may issue a ResourceWarning on exit.
                await asyncio.sleep(0)

    async def __aenter__(self) -> 'APIToolRegistry':
        await self._ensure_client()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
