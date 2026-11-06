"""FastAPI 应用工厂；资源由 lifespan 显式关闭。"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ms_agent.agent.api import AgentService
from ms_agent.agent.protocol import API_PREFIX, SERVICE_VERSION
from ms_agent.agent.recipe import RecipeService
from ms_agent.agent.run_service import RunError, RunService
from ms_agent.llm.http_client import HTTPLLMClient
from ms_agent.session.locking import PersistenceError
from ms_agent.session.run_store import FileRunStore, ProtocolVersionUnsupported, StorageCorrupted
from service.aigc.dependencies import User
from service.aigc.routers import recipes, runs
from service.aigc.settings import Settings
from service.aigc.telemetry import Telemetry, configure_logging


def error_response(request_id: str, code: str, status: int, details=None) -> JSONResponse:
    return JSONResponse(
        {'code': code, 'message': code, 'request_id': request_id, 'details': details}, status_code=status
    )


class RequestBoundary:
    """纯 ASGI 边界，避免普通 HTTP 中间件取消长连接或后台执行。"""

    def __init__(self, app, application: FastAPI) -> None:
        self.app, self.application = app, application

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request_id = uuid4().hex
        scope.setdefault('state', {})['request_id'] = request_id
        settings = self.application.state.settings
        telemetry = getattr(self.application.state, 'telemetry', None)
        if telemetry:
            telemetry.http_requests += 1
        limit = settings.server.max_body_bytes
        if scope['method'] in {'POST', 'PUT', 'PATCH'}:
            body = bytearray()
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                body.extend(message.get('body', b''))
                if len(body) > limit:
                    if telemetry:
                        telemetry.http_errors += 1
                    response = error_response(request_id, 'REQUEST_TOO_LARGE', 413)
                    response.headers['X-Request-Id'] = request_id
                    return await response(scope, receive, send)
                if not message.get('more_body'):
                    break
            consumed = False
            original_receive = receive

            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await original_receive()

            receive = replay

        async def tagged_send(message):
            if message['type'] == 'http.response.start':
                if telemetry and message['status'] >= 400:
                    telemetry.http_errors += 1
                message['headers'] = [*message.get('headers', []), (b'x-request-id', request_id.encode())]
            await send(message)

        started = time.monotonic()
        await self.app(scope, receive, tagged_send)
        logging.getLogger(__name__).info(
            '%s %s %.3fs',
            scope['method'],
            scope['path'],
            time.monotonic() - started,
            extra={
                'request_id': request_id,
                'run_uuid': scope.get('state', {}).get('run_uuid') or scope.get('path_params', {}).get('run_uuid'),
            },
        )


def create_app(settings: Settings | None = None, *, llm_client=None, recipe_llm=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        effective = settings or Settings.load()
        application.state.settings = effective
        agent_config = effective.to_agent_config()
        credentials = effective.credentials
        store = FileRunStore(
            effective.storage.data_dir,
            cache_entries=effective.storage.cache_entries,
            cache_bytes=effective.storage.cache_bytes,
            projection=effective.storage.projection,
            secret_values=(
                credentials.service.get_secret_value(),
                credentials.llm.get_secret_value(),
                credentials.api.get_secret_value(),
            ),
        )
        executor = ThreadPoolExecutor(max_workers=effective.storage.io_workers, thread_name_prefix='aigc-io')
        store.locks.executor = executor
        store.lease.check_filesystem(effective.storage.lock_policy)
        if effective.observability.json_logs:
            configure_logging(store.secret_values)
        logging.getLogger(__name__).info('配置已加载 %s', effective.config_digest)
        agent = AgentService(agent_config, locks=store.locks, llm_client=llm_client)
        runtime = RunService(
            store,
            agent,
            runtime_config=effective.runtime.model_dump(),
            event_config=effective.events.model_dump(),
            lease_wait=effective.storage.lease_wait,
        )
        application.state.runs = runtime
        builder = None
        telemetry = Telemetry(effective, runtime)
        application.state.telemetry = telemetry
        application.root_path = effective.server.public_path_prefix
        try:
            await runtime.initialize()
            builder = RecipeService(
                runtime,
                recipe_llm
                or HTTPLLMClient(
                    agent_config['llm'],
                    agent._http_session,
                    limiter=agent.limiters['llm'],
                ),
                timeout=effective.recipe.timeout,
                concurrency=effective.recipe.concurrency,
                max_inflight=effective.recipe.max_inflight,
            )
            application.state.recipes = builder
            telemetry.start()
            yield
        finally:
            runtime.begin_drain()
            await telemetry.close()
            if runtime._operations:
                await asyncio.gather(*runtime._operations, return_exceptions=True)
            if builder is not None:
                await builder.close()
            try:
                await runtime.close(effective.runtime.drain_timeout)
            finally:
                executor.shutdown(wait=True)

    application = FastAPI(title='MS-Agent AIGC Service', version=SERVICE_VERSION, lifespan=lifespan)
    application.add_middleware(RequestBoundary, application=application)
    application.include_router(runs.router, prefix=API_PREFIX)
    application.include_router(recipes.router, prefix=API_PREFIX)
    default_openapi = application.openapi

    def openapi():
        schema = default_openapi()
        effective = getattr(application.state, 'settings', settings)
        if effective and effective.auth.header.lower() != 'authorization':
            schemes = schema.setdefault('components', {}).setdefault('securitySchemes', {})
            schemes['AIGCServiceToken'] = {'type': 'apiKey', 'in': 'header', 'name': effective.auth.header}
            for path in schema['paths'].values():
                for operation in path.values():
                    if isinstance(operation, dict) and operation.get('security'):
                        operation['security'] = [{'AIGCServiceToken': []}]
        return schema

    application.openapi = openapi

    @application.exception_handler(RunError)
    async def run_error(request: Request, exc: RunError):
        response = error_response(request.state.request_id, exc.code, exc.status, exc.details)
        if exc.status == 429:
            response.headers['Retry-After'] = str(application.state.settings.runtime.retry_after)
        return response

    @application.exception_handler(ProtocolVersionUnsupported)
    async def protocol_error(request: Request, exc: ProtocolVersionUnsupported):
        return error_response(request.state.request_id, 'PROTOCOL_VERSION_UNSUPPORTED', 409)

    @application.exception_handler(StorageCorrupted)
    async def corrupted_error(request: Request, exc: StorageCorrupted):
        return error_response(request.state.request_id, 'STORAGE_CORRUPTED', 503)

    @application.exception_handler(PersistenceError)
    async def storage_error(request: Request, exc: PersistenceError):
        return error_response(request.state.request_id, 'STORAGE_UNAVAILABLE', 503)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [{'location': list(error['loc']), 'type': error['type']} for error in exc.errors()]
        if request.url.path.endswith('/events') and any(item['location'] == ['query', 'after_seq'] for item in details):
            return error_response(request.state.request_id, 'EVENT_CURSOR_INVALID', 400, details)
        return error_response(request.state.request_id, 'INPUT_INVALID', 422, details)

    @application.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError):
        return error_response(request.state.request_id, 'INPUT_INVALID', 400)

    @application.get('/health/live', tags=['Health'])
    async def live():
        runtime = application.state.runs
        return JSONResponse(
            {'status': 'live' if runtime.store.healthy else 'unavailable'},
            status_code=200 if runtime.store.healthy else 503,
        )

    @application.get('/health/ready', tags=['Health'])
    async def ready():
        runtime = application.state.runs
        healthy = runtime.accepting and runtime.store.healthy and runtime.store.lease.held
        return JSONResponse(
            {
                'status': 'ready' if healthy else 'unavailable',
                'config_digest': application.state.settings.config_digest,
            },
            status_code=200 if healthy else 503,
        )

    @application.get('/internal/stats', tags=['Internal'])
    async def stats(user: User):
        return application.state.telemetry.stats()

    return application
