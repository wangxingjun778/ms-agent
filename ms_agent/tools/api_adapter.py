"""API Tool Adapter — wraps HTTP API endpoints as agent tools.

This module provides the lightweight-core replacement for ms-agent's built-in
tools: instead of running tool logic in-process, every tool declares a HTTP
endpoint (sync or async-task) and the adapter translates the standard
OpenAI function-calling payload into an HTTP request, polls if needed, and
extracts the result via a configured dot-path.

The design mirrors the ModelScope API-Inference image generation flow
(submit → task_id → poll /v1/tasks/{task_id}) so one adapter can describe
both plain RPC-style endpoints and long-running task endpoints.

Note
----
A richer :class:`ToolResult` lives in :mod:`ms_agent.llm.utils` (field name
``text``). This module intentionally defines its own :class:`ToolResult`
with ``content``/``raw_data``/``metadata`` because the API-tool layer runs
*before* integration with the LLM runtime and we don't want a transitive
import on the LLM package. If ``ms_agent.tools.schema`` is introduced
later, ToolResult should be re-exported from there and this definition
removed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    import aiohttp  # type: ignore

    _HAS_AIOHTTP = True
except ImportError:  # pragma: no cover — aiohttp is in requirements/framework.txt
    aiohttp = None  # type: ignore
    _HAS_AIOHTTP = False

logger = logging.getLogger(__name__)

__all__ = [
    'AuthConfig',
    'APIConfig',
    'OutputMapping',
    'APIToolSpec',
    'ToolResult',
    'APIToolAdapter',
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class AuthConfig:
    """Authentication configuration for one tool or the whole registry.

    ``type`` controls both whether an auth header is added and how it is
    formatted:

    * ``"bearer"``   — ``Authorization: Bearer <token>``
    * ``"api_key"``  — ``<header_name>: <header_prefix> <token>`` (prefix may
      be empty, e.g. a plain ``X-Api-Key: <token>``)
    * ``"none"``     — no auth header is added

    The token is resolved lazily via :meth:`resolve_token` so a config file
    can carry an env-var *name* without reading the environment at import
    time (useful for tests and for config validation on hosts where the
    token is not yet provisioned).
    """

    type: str = 'none'
    token: Optional[str] = None
    token_env: Optional[str] = None
    header_name: str = 'Authorization'
    header_prefix: str = 'Bearer'

    def resolve_token(self) -> Optional[str]:
        """Return the concrete token value, or ``None`` if unavailable.

        Direct ``token`` wins over ``token_env`` so a caller can override
        the configured env-var for a specific invocation.
        """
        if self.token:
            return self.token
        if self.token_env:
            value = os.environ.get(self.token_env)
            if value:
                return value
            logger.warning(
                'AuthConfig: env var %r is not set; auth header will be omitted',
                self.token_env,
            )
        return None


@dataclass
class APIConfig:
    """Transport-level configuration for a single tool endpoint.

    ``async_mode`` distinguishes two call shapes:

    * ``False`` — one HTTP request, response body carries the result.
    * ``True``  — the first request submits a task and returns a ``task_id``;
      :meth:`APIToolAdapter._poll_task` then GETs ``poll_path`` (with the
      ``{task_id}`` placeholder substituted) every ``poll_interval`` seconds
      until the extracted status is terminal or ``poll_timeout`` elapses.
    """

    method: str = 'POST'
    path: str = ''
    async_mode: bool = False
    poll_path: Optional[str] = None
    poll_interval: float = 5.0
    poll_timeout: float = 600.0
    headers: Dict[str, str] = field(default_factory=dict)
    timeout: float = 120.0


@dataclass
class OutputMapping:
    """Dot-path specification for extracting result/error fields.

    Dot paths support the three shapes handled by
    :meth:`APIToolAdapter._extract_value`: plain nested access
    (``"a.b.c"``), index selection (``"a.b[0]"``), and wildcard collection
    (``"a.b[*].c"``).

    For sync tools only ``success_path`` and ``error_path`` are consulted;
    async tools additionally use ``task_id_path``, ``status_path``, and the
    success/failure status sets to drive the poll loop.
    """

    success_path: Optional[str] = None
    error_path: str = 'errors.message'
    task_id_path: str = 'output.task_id'
    status_path: str = 'output.task_status'
    success_statuses: List[str] = field(default_factory=lambda: ['SUCCEED'])
    failure_statuses: List[str] = field(default_factory=lambda: ['FAILED', 'TIMEOUT'])


@dataclass
class APIToolSpec:
    """Full declaration of one API-backed tool.

    ``parameters`` is the JSON Schema exposed verbatim to the LLM via
    :meth:`APIToolAdapter.get_tool_spec` — i.e. this is the contract the
    model sees. ``api_config``/``output_mapping`` describe how to translate
    the model's call into HTTP and the HTTP response back into a tool
    message, and must stay internal.
    """

    name: str
    description: str
    api_config: APIConfig
    parameters: Dict[str, Any]
    output_mapping: OutputMapping
    auth: Optional[AuthConfig] = None

    def __post_init__(self) -> None:
        # Mirror ms_agent/tools/tool_manager.py's MAX_TOOL_NAME_LEN=64 contract
        # so a mis-named tool is caught at load time instead of silently being
        # dropped by the dispatcher later.
        if len(self.name) > 64:
            raise ValueError(f'APIToolSpec.name must be <= 64 chars, got {len(self.name)}: {self.name!r}')


@dataclass
class ToolResult:
    """Local tool-execution outcome for API tools.

    See the module docstring for the relationship to
    :class:`ms_agent.llm.utils.ToolResult`.
    """

    tool_name: str
    success: bool
    content: str
    raw_data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_error(cls, tool_name: str, error_msg: str, **metadata: Any) -> 'ToolResult':
        return cls(
            tool_name=tool_name,
            success=False,
            content=error_msg,
            error=error_msg,
            metadata=metadata,
        )

    @classmethod
    def from_success(
        cls, tool_name: str, content: str, raw_data: Optional[Dict[str, Any]] = None, **metadata: Any
    ) -> 'ToolResult':
        return cls(
            tool_name=tool_name,
            success=True,
            content=content,
            raw_data=raw_data,
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

_PATH_SEGMENT = re.compile(r'([^.\[\]]+)|\[(\*|\d+)\]')


class APIToolAdapter:
    """Wraps a single HTTP API endpoint as a standard tool call.

    The adapter is deliberately thin: it does not retry on 5xx, does not
    cache task results, and does not reshape the response beyond what
    :class:`OutputMapping` describes. Those behaviours live in the
    registry (lifecycle) and higher-level agent runtime (retry policy).

    The HTTP client is injected rather than created so the enclosing
    :class:`APIToolRegistry` can share one TCP pool across every tool and
    close it exactly once; passing ``None`` falls back to urllib on a
    thread, which is only intended for the dependency-missing path.
    """

    def __init__(
        self,
        spec: APIToolSpec,
        base_url: str,
        default_auth: AuthConfig,
        http_client: Optional['aiohttp.ClientSession'] = None,
    ) -> None:
        self._spec = spec
        self._base_url = base_url.rstrip('/')
        self._auth = spec.auth or default_auth
        self._client = http_client

    # ---- Public spec surface -------------------------------------------------

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def description(self) -> str:
        return self._spec.description

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return self._spec.parameters

    def get_tool_spec(self) -> Dict[str, Any]:
        """Return the OpenAI function-calling-format tool spec.

        The shape is identical to what MCP/built-in tools return via
        :meth:`ToolBase.get_tools`, so API tools plug into the same
        dispatcher without special casing.
        """
        return {
            'type': 'function',
            'function': {
                'name': self._spec.name,
                'description': self._spec.description,
                'parameters': self._spec.parameters,
            },
        }

    # ---- Execution -----------------------------------------------------------

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        """Execute the API call. Routes to sync or async mode."""
        started = time.monotonic()
        try:
            if self._spec.api_config.async_mode:
                result = await self._async_call(args)
            else:
                result = await self._sync_call(args)
        except asyncio.TimeoutError as exc:
            return ToolResult.from_error(
                self._spec.name,
                f'API call timed out: {exc}',
                duration=time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 — surface any transport error
            logger.exception('API tool %s failed', self._spec.name)
            return ToolResult.from_error(
                self._spec.name,
                f'{type(exc).__name__}: {exc}',
                duration=time.monotonic() - started,
            )
        result.metadata.setdefault('duration', time.monotonic() - started)
        return result

    async def _sync_call(self, args: Dict[str, Any]) -> ToolResult:
        """Single HTTP request, extract success/error directly from body."""
        url = self._build_url(self._spec.api_config.path)
        headers = self._build_headers()
        data = await self._request(
            self._spec.api_config.method, url, headers, json_body=args, timeout=self._spec.api_config.timeout
        )
        return self._shape_result(data)

    async def _async_call(self, args: Dict[str, Any]) -> ToolResult:
        """Submit task then poll until a terminal status is observed."""
        if not self._spec.api_config.poll_path:
            return ToolResult.from_error(
                self._spec.name,
                'async_mode=true but api.poll_path is not configured',
            )
        submit_url = self._build_url(self._spec.api_config.path)
        headers = self._build_headers()
        submit_resp = await self._request(
            self._spec.api_config.method, submit_url, headers, json_body=args, timeout=self._spec.api_config.timeout
        )

        task_id = self._extract_value(submit_resp, self._spec.output_mapping.task_id_path)
        # Some task APIs put task_id at the top level instead of under
        # ``output`` — fall back to the common top-level key before giving up,
        # so a legitimate submission isn't rejected over a path mismatch.
        if not task_id and 'task_id' in submit_resp:
            task_id = submit_resp.get('task_id')
        if not task_id:
            err = self._extract_value(submit_resp, self._spec.output_mapping.error_path)
            return ToolResult.from_error(
                self._spec.name,
                f'Task submission did not return task_id: {err or submit_resp}',
                raw=submit_resp,
            )

        logger.info('API tool %s submitted async task %s', self._spec.name, task_id)
        final = await self._poll_task(str(task_id))
        result = self._shape_result(final)
        result.metadata['task_id'] = str(task_id)
        return result

    async def _poll_task(self, task_id: str) -> Dict[str, Any]:
        """Poll until status is terminal or ``poll_timeout`` elapses."""
        poll_path = self._spec.api_config.poll_path or ''
        url = self._build_url(poll_path, task_id=task_id)
        headers = self._build_headers()
        # Poll endpoints are almost always GET; a few schemes use POST but
        # the ``method`` field describes the *submit* call, not the poll —
        # keep polling GET-only unless/until a real counterexample shows up.
        deadline = time.monotonic() + self._spec.api_config.poll_timeout
        interval = max(0.1, float(self._spec.api_config.poll_interval))
        last = {}
        while True:
            data = await self._request('GET', url, headers, json_body=None, timeout=self._spec.api_config.timeout)
            last = data if isinstance(data, dict) else {'raw': data}
            status = self._extract_value(last, self._spec.output_mapping.status_path)
            if status in self._spec.output_mapping.success_statuses:
                return last
            if status in self._spec.output_mapping.failure_statuses:
                return last
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last['_poll_timeout'] = True
                return last
            await asyncio.sleep(min(interval, remaining))

    # ---- HTTP plumbing -------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        json_body: Optional[Dict[str, Any]],
        timeout: float,
    ) -> Dict[str, Any]:
        """Perform one HTTP request and parse the JSON body.

        Non-JSON responses are wrapped as ``{"_raw": "<text>"}`` so callers
        downstream can still surface them via ``error_path`` instead of
        crashing on a parse error.
        """
        if self._client is not None and _HAS_AIOHTTP:
            tmo = aiohttp.ClientTimeout(total=timeout)
            async with self._client.request(method.upper(), url, headers=headers, json=json_body, timeout=tmo) as resp:
                text = await resp.text()
                return _parse_json_or_wrap(text, resp.status)
        # Fallback path: no shared aiohttp client. Keep the signature async
        # by delegating a blocking urllib call to a worker thread so the
        # event loop isn't stalled on sockets.
        return await asyncio.to_thread(_urllib_request, method, url, headers, json_body, timeout)

    def _shape_result(self, data: Dict[str, Any]) -> ToolResult:
        """Translate a parsed response into a :class:`ToolResult`.

        A non-empty ``error_path`` value always wins over a success path —
        services like the referenced image API return HTTP 200 with
        ``task_status=FAILED`` + ``errors.message``, so trusting the HTTP
        status alone would surface a success for a logically failed task.
        """
        if data.get('_poll_timeout'):
            return ToolResult.from_error(
                self._spec.name,
                f'Polling exceeded {self._spec.api_config.poll_timeout:.0f}s without a terminal status',
                raw=data,
            )

        err = self._extract_value(data, self._spec.output_mapping.error_path)
        if err:
            msg = err if isinstance(err, str) else json.dumps(err, ensure_ascii=False)
            return ToolResult.from_error(self._spec.name, msg, raw=data)

        success_path = self._spec.output_mapping.success_path
        if success_path:
            value = self._extract_value(data, success_path)
            if value is None:
                return ToolResult.from_error(
                    self._spec.name,
                    f'Response missing success field at {success_path!r}',
                    raw=data,
                )
            content = _stringify(value)
        else:
            content = json.dumps(data, ensure_ascii=False)
        return ToolResult.from_success(self._spec.name, content, raw_data=data)

    def _build_url(self, path: str, **placeholders: Any) -> str:
        """Join ``base_url`` and ``path``, substituting ``{foo}`` placeholders."""
        if placeholders:
            try:
                path = path.format(**placeholders)
            except KeyError as exc:
                raise ValueError(f'Missing placeholder {exc!s} for path {path!r}') from exc
        if not path.startswith('/'):
            path = '/' + path
        return f'{self._base_url}{path}'

    def _build_headers(self) -> Dict[str, str]:
        """Build request headers: Content-Type + auth + per-tool extras."""
        headers: Dict[str, str] = {'Content-Type': 'application/json'}
        auth_type = (self._auth.type or 'none').lower()
        if auth_type != 'none':
            token = self._auth.resolve_token()
            if token:
                if auth_type == 'bearer':
                    headers['Authorization'] = f'Bearer {token}'
                elif auth_type == 'api_key':
                    prefix = (self._auth.header_prefix or '').strip()
                    value = f'{prefix} {token}'.strip() if prefix else token
                    headers[self._auth.header_name] = value
                else:
                    logger.warning('Unknown auth type %r, skipping header', auth_type)
        if self._spec.api_config.headers:
            headers.update(self._spec.api_config.headers)
        return headers

    @staticmethod
    def _extract_value(data: Any, path: str) -> Any:
        """Extract a value from a nested structure using dot-path notation.

        Supported syntax:

        * ``"a.b.c"``         — nested dict access
        * ``"a.b[0]"``        — list index access
        * ``"a.b[*].c"``      — wildcard: returns a list of ``item["c"]``
          for every item in the matched list (missing keys become ``None``)
        """
        if not path or data is None:
            return None
        current: Any = data
        tokens = _PATH_SEGMENT.findall(path)
        for i, (key, idx) in enumerate(tokens):
            if key:
                if not isinstance(current, dict):
                    return None
                current = current.get(key)
            elif idx == '*':
                if not isinstance(current, list):
                    return None
                # Everything *after* the wildcard applies to each element.
                remainder = _rebuild_path(tokens[i + 1 :])
                if not remainder:
                    return list(current)
                return [APIToolAdapter._extract_value(item, remainder) for item in current]
            else:
                if not isinstance(current, list):
                    return None
                try:
                    current = current[int(idx)]
                except (IndexError, ValueError):
                    return None
            if current is None:
                return None
        return current


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rebuild_path(tokens: List[tuple]) -> str:
    """Reconstruct a dot-path string from tokens produced by _PATH_SEGMENT.

    Only used after a wildcard, so leading dots are stripped to keep the
    recursive call's grammar consistent (``.c`` would never parse).
    """
    out = []
    for key, idx in tokens:
        if key:
            out.append(('.' if out else '') + key)
        elif idx:
            out.append(f'[{idx}]')
    return ''.join(out)


def _stringify(value: Any) -> str:
    """Produce the LLM-facing string for an extracted value.

    Lists of plain strings read better as a JSON array than ``repr``, and
    dicts should always be JSON so the model can walk the structure.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _parse_json_or_wrap(text: str, status: int) -> Dict[str, Any]:
    """Parse JSON, otherwise wrap the raw body so the caller still sees it."""
    try:
        parsed = json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {
            '_raw': text,
            'errors': {
                'message': f'Non-JSON response (HTTP {status})',
            },
        }
    if isinstance(parsed, dict):
        return parsed
    return {'data': parsed}


def _urllib_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    json_body: Optional[Dict[str, Any]],
    timeout: float,
) -> Dict[str, Any]:
    """Blocking fallback used when aiohttp is unavailable."""
    import urllib.error
    import urllib.request

    body_bytes = json.dumps(json_body, ensure_ascii=False).encode('utf-8') if json_body is not None else None
    req = urllib.request.Request(url=url, data=body_bytes, method=method.upper())
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode('utf-8', errors='replace')
            status = resp.status
    except urllib.error.HTTPError as exc:
        text = exc.read().decode('utf-8', errors='replace') if exc.fp else ''
        status = exc.code
    return _parse_json_or_wrap(text, status)
