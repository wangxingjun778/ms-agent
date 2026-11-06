"""API Tool Adapter — wraps HTTP API endpoints as agent tools.

This module provides the lightweight-core replacement for ms-agent's built-in
tools: instead of running tool logic in-process, every tool declares a HTTP
endpoint (sync or async-task) and the adapter translates the standard
OpenAI function-calling payload into an HTTP request, polls if needed, and
extracts the result via a configured dot-path.

The design mirrors the ModelScope API-Inference image generation flow
(submit → task_id → poll /v1/tasks/{task_id}) so one adapter can describe
both plain RPC-style endpoints and long-running task endpoints.

工具结果统一使用 ``ms_agent.tools.schema.ToolResult``，传输统一使用 aiohttp。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp

from ms_agent.session.locking import PersistenceError
from ms_agent.tools.schema import SubmissionNotSent, ToolInvocationContext, ToolResult
from ms_agent.utils.limiter import retry_delay

logger = logging.getLogger(__name__)
_CONNECTOR_ERRORS = (aiohttp.ClientConnectorError,)
_POLL_ERRORS = (OSError, asyncio.TimeoutError, aiohttp.ClientError)

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
    poll_max_interval: float = 5.0
    poll_factor: float = 1.0


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
    requires_approval: bool = False

    def __post_init__(self) -> None:
        # Mirror ms_agent/tools/tool_manager.py's MAX_TOOL_NAME_LEN=64 contract
        # so a mis-named tool is caught at load time instead of silently being
        # dropped by the dispatcher later.
        if len(self.name) > 64:
            raise ValueError(f'APIToolSpec.name must be <= 64 chars, got {len(self.name)}: {self.name!r}')


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

    HTTP 客户端由 APIToolRegistry 注入并管理生命周期，所有工具共享连接池。
    不提供依赖缺失时的阻塞传输回退；缺失依赖在启动阶段失败。
    """

    def __init__(
        self,
        spec: APIToolSpec,
        base_url: str,
        default_auth: AuthConfig,
        http_client: 'aiohttp.ClientSession',
    ) -> None:
        self._spec = spec
        self._base_url = base_url.rstrip('/')
        self._auth = spec.auth or default_auth
        self._client = http_client
        self.poll_client = None
        self.limiters = {}
        self.upstreams = {}

    @asynccontextmanager
    async def _slot(self, method):
        limiter = self.limiters.get('poll' if method.upper() == 'GET' else 'submit')
        if limiter:
            async with limiter:
                yield
        else:
            yield

    async def _submit(self, url, headers, args, context=None):
        policy = self.upstreams.get('submit', {}).get('retry', {'attempts': 1})

        async def send():
            for attempt in range(policy['attempts']):
                if context:
                    await context.emit('submission_intent', {'tool_name': self.name, 'arguments': args})
                try:
                    data = await self._request(
                        self._spec.api_config.method,
                        url,
                        headers,
                        json_body=args,
                        timeout=self._spec.api_config.timeout,
                        context=context,
                    )
                    if data.get('_http_status', 200) != 429 or attempt + 1 == policy['attempts']:
                        return data
                    retry_headers = {'Retry-After': data.get('_retry_after', '')}
                except _CONNECTOR_ERRORS:
                    if attempt + 1 == policy['attempts']:
                        raise
                    retry_headers = {}
                await asyncio.sleep(retry_delay(retry_headers, attempt, policy))

        return await asyncio.wait_for(send(), policy.get('budget', self._spec.api_config.timeout))

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

    async def execute(self, args: Dict[str, Any], context: Optional[ToolInvocationContext] = None) -> ToolResult:
        """Execute the API call. Routes to sync or async mode."""
        started = time.monotonic()
        try:
            if self._spec.api_config.async_mode:
                result = await self._async_call(args, context)
            else:
                result = await self._sync_call(args, context)
        except PersistenceError:
            raise
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

    async def _sync_call(self, args: Dict[str, Any], context: Optional[ToolInvocationContext] = None) -> ToolResult:
        """Single HTTP request, extract success/error directly from body."""
        url = self._build_url(self._spec.api_config.path)
        headers = self._build_headers(context)
        if context:
            await context.emit('submission_intent', {'tool_name': self.name, 'arguments': args})
        try:
            data = await self._request(
                self._spec.api_config.method,
                url,
                headers,
                json_body=args,
                timeout=self._spec.api_config.timeout,
                context=context,
            )
        except (SubmissionNotSent, PersistenceError):
            raise
        except BaseException:
            if context:
                await context.emit('delivery_unknown', {'tool_name': self.name, 'remote_cancel_supported': False})
            raise
        if data.get('_http_status', 200) >= 500 or data.get('_http_status') == 408:
            if context:
                await context.emit('delivery_unknown', {'tool_name': self.name, 'remote_cancel_supported': False})
            return ToolResult.from_error(self.name, '外部提交结果未确认', delivery_unknown=True)
        return self._shape_result(data)

    async def _async_call(self, args: Dict[str, Any], context: Optional[ToolInvocationContext] = None) -> ToolResult:
        """Submit task then poll until a terminal status is observed."""
        if not self._spec.api_config.poll_path:
            return ToolResult.from_error(
                self._spec.name,
                'async_mode=true but api.poll_path is not configured',
            )
        submit_url = self._build_url(self._spec.api_config.path)
        headers = self._build_headers(context)
        try:
            submit_resp = await self._submit(submit_url, headers, args, context)
        except (SubmissionNotSent, PersistenceError):
            raise
        except _CONNECTOR_ERRORS:
            return ToolResult.from_error(self.name, '连接未建立，请求未发送')
        except BaseException as exc:
            if context is not None:
                await context.emit('delivery_unknown', {'tool_name': self.name, 'remote_cancel_supported': False})
            if isinstance(exc, asyncio.CancelledError):
                raise
            return ToolResult.from_error(self.name, '外部提交结果未确认', delivery_unknown=True)
        http_status = submit_resp.get('_http_status', 200)
        if http_status >= 500 or http_status == 408:
            if context is not None:
                await context.emit('delivery_unknown', {'tool_name': self.name, 'remote_cancel_supported': False})
            return ToolResult.from_error(self.name, '外部提交结果未确认', raw=submit_resp, delivery_unknown=True)
        if (
            submit_resp.get('_http_status', 200) >= 400
            or self._extract_value(submit_resp, self._spec.output_mapping.status_path)
            in self._spec.output_mapping.failure_statuses
        ):
            return self._shape_result(submit_resp)

        task_id = self._extract_value(submit_resp, self._spec.output_mapping.task_id_path)
        if not task_id:
            err = self._extract_value(submit_resp, self._spec.output_mapping.error_path)
            if context is not None:
                await context.emit('delivery_unknown', {'tool_name': self.name, 'remote_cancel_supported': False})
            return ToolResult.from_error(
                self._spec.name,
                f'Task submission did not return task_id: {err or submit_resp}',
                raw=submit_resp,
                delivery_unknown=True,
            )

        if context is not None:
            await context.emit(
                'task_submitted',
                {
                    'tool_name': self.name,
                    'task_id': str(task_id),
                    'request_id': submit_resp.get('request_id'),
                    'task_status': submit_resp.get('task_status'),
                    'remote_cancel_supported': False,
                },
            )
        try:
            final = await self._poll_task(str(task_id), context)
        except Exception:
            return ToolResult.from_error(self.name, '外部任务查询未完成', task_id=str(task_id), unresolved=True)
        result = self._shape_result(final)
        result.metadata['task_id'] = str(task_id)
        result.metadata['unresolved'] = bool(
            final.get('_poll_timeout')
            or final.get('_invalid_status')
            or (
                final.get('_http_status', 200) >= 400
                and self._extract_value(final, self._spec.output_mapping.status_path)
                not in self._spec.output_mapping.failure_statuses
            )
        )
        return result

    async def query_task(self, task_id: str):
        """只查询原任务；返回 pending=None、确认结果或未确认错误，绝不 POST。"""
        if not self._spec.api_config.poll_path:
            return ToolResult.from_error(self.name, '工具不支持原任务查询', unresolved=True)
        url = self._build_url(self._spec.api_config.poll_path, task_id=quote(str(task_id), safe=''))
        data = await self._request(
            'GET', url, self._build_headers(None), json_body=None, timeout=self._spec.api_config.timeout
        )
        status = self._extract_value(data, self._spec.output_mapping.status_path)
        if data.get('_http_status', 200) == 429 or data.get('_http_status', 200) >= 500:
            return ToolResult.from_error(self.name, '原任务查询暂不可用', unresolved=True)
        if status in ('PENDING', 'PROCESSING', 'RUNNING'):
            return None
        result = self._shape_result(data)
        result.metadata['unresolved'] = (
            status not in self._spec.output_mapping.success_statuses + self._spec.output_mapping.failure_statuses
        )
        return result

    async def _poll_task(self, task_id: str, context: Optional[ToolInvocationContext] = None) -> Dict[str, Any]:
        """Poll until status is terminal or ``poll_timeout`` elapses."""
        poll_path = self._spec.api_config.poll_path or ''
        url = self._build_url(poll_path, task_id=quote(task_id, safe=''))
        headers = self._build_headers(context)
        # Poll endpoints are almost always GET; a few schemes use POST but
        # the ``method`` field describes the *submit* call, not the poll —
        # keep polling GET-only unless/until a real counterexample shows up.
        policy = self.upstreams.get('poll', {}).get('retry', {'initial': 1, 'maximum': 30})
        deadline = time.monotonic() + min(
            self._spec.api_config.poll_timeout, policy.get('budget', self._spec.api_config.poll_timeout)
        )
        interval = max(0.1, float(self._spec.api_config.poll_interval))
        last, failures = {}, 0
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                last = await asyncio.wait_for(
                    self._request(
                        'GET', url, headers, json_body=None, timeout=min(self._spec.api_config.timeout, remaining)
                    ),
                    remaining,
                )
                status_code = last.get('_http_status', 200)
            except _POLL_ERRORS:
                status_code = 503
                last = {}
            if status_code == 429 or status_code >= 500:
                delay = retry_delay({'Retry-After': last.get('_retry_after', '')}, min(failures, 10), policy)
                failures += 1
            else:
                failures = 0
                if status_code >= 400 or self._extract_value(last, self._spec.output_mapping.error_path):
                    return last
                status = self._extract_value(last, self._spec.output_mapping.status_path)
                if (
                    status in self._spec.output_mapping.success_statuses
                    or status in self._spec.output_mapping.failure_statuses
                ):
                    return last
                if status not in ('PENDING', 'PROCESSING', 'RUNNING'):
                    last['_invalid_status'] = True
                    return last
                delay = interval
                interval = min(
                    max(self._spec.api_config.poll_max_interval, interval), interval * self._spec.api_config.poll_factor
                )
            await asyncio.sleep(min(delay, max(0, deadline - time.monotonic())))
        last['_poll_timeout'] = True
        return last

    # ---- HTTP plumbing -------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        json_body: Optional[Dict[str, Any]],
        timeout: float,
        context: Optional[ToolInvocationContext] = None,
    ) -> Dict[str, Any]:
        """Perform one HTTP request and parse the JSON body.

        Non-JSON responses are wrapped as ``{"_raw": "<text>"}`` so callers
        downstream can still surface them via ``error_path`` instead of
        crashing on a parse error.
        """
        client = self.poll_client if method.upper() == 'GET' and self.poll_client is not None else self._client
        if client is not None:
            options = self.upstreams.get('poll' if method.upper() == 'GET' else 'submit', {})
            tmo = aiohttp.ClientTimeout(
                total=timeout, connect=options.get('connect_timeout'), sock_read=options.get('read_timeout')
            )
            started = False
            try:
                async with self._slot(method):
                    if context and context.guard:
                        await context.guard()
                    started = True
                    async with client.request(
                        method.upper(), url, headers=headers, json=json_body, timeout=tmo
                    ) as resp:
                        text = await resp.text()
                        data = _parse_json_or_wrap(text, resp.status)
                        if 'Retry-After' in resp.headers:
                            data['_retry_after'] = resp.headers['Retry-After']
                        return data
            except asyncio.CancelledError:
                if context and not started:
                    await context.emit('submission_not_sent', {'tool_name': self.name})
                    raise SubmissionNotSent from None
                raise
        raise RuntimeError('API 工具需要生命周期所有者注入 aiohttp ClientSession')

    def _shape_result(self, data: Dict[str, Any]) -> ToolResult:
        """Translate a parsed response into a :class:`ToolResult`.

        A non-empty ``error_path`` value always wins over a success path —
        services like the referenced image API return HTTP 200 with
        ``task_status=FAILED`` + ``errors.message``, so trusting the HTTP
        status alone would surface a success for a logically failed task.
        """
        status = data.get('_http_status', 200)
        if (
            status >= 400
            or data.get('_invalid_status')
            or self._extract_value(data, self._spec.output_mapping.status_path)
            in self._spec.output_mapping.failure_statuses
        ):
            return ToolResult.from_error(self.name, '外部 API 请求失败', raw_data=data, http_status=status)
        if data.get('_poll_timeout'):
            return ToolResult.from_error(
                self._spec.name,
                f'Polling exceeded {self._spec.api_config.poll_timeout:.0f}s without a terminal status',
                raw_data=data,
            )

        err = self._extract_value(data, self._spec.output_mapping.error_path)
        if err:
            msg = err if isinstance(err, str) else json.dumps(err, ensure_ascii=False)
            return ToolResult.from_error(self._spec.name, msg, raw_data=data)

        success_path = self._spec.output_mapping.success_path
        if success_path:
            value = self._extract_value(data, success_path)
            if value is None or value == []:
                return ToolResult.from_error(
                    self._spec.name,
                    f'Response missing success field at {success_path!r}',
                    raw_data=data,
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

    def _build_headers(self, context: Optional[ToolInvocationContext] = None) -> Dict[str, str]:
        """Build request headers: Content-Type + auth + per-tool extras."""
        headers: Dict[str, str] = {'Content-Type': 'application/json'}
        auth_type = (self._auth.type or 'none').lower()
        if auth_type != 'none':
            token = context.credentials.get(self.name) if context is not None else None
            token = token or self._auth.resolve_token()
            if not token:
                raise ValueError('外部 API 凭据未配置')
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
            '_http_status': status,
            '_raw': text,
            'errors': {
                'message': f'Non-JSON response (HTTP {status})',
            },
        }
    if isinstance(parsed, dict):
        return {**parsed, '_http_status': status}
    return {'data': parsed, '_http_status': status}
