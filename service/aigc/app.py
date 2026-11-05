"""FastAPI 应用工厂；资源由 lifespan 显式关闭。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ms_agent.agent.api import AgentService
from ms_agent.agent.recipe import RecipeService
from ms_agent.agent.run_service import RunError, RunService
from ms_agent.llm.http_client import HTTPLLMClient
from ms_agent.session.locking import PersistenceError
from ms_agent.session.run_store import FileRunStore
from service.aigc.routers import recipes, runs
from service.aigc.settings import Settings, read_secret


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
        settings = getattr(self.application.state, 'settings', None)
        limit = settings.max_body_bytes if settings else 2 * 1024 * 1024
        if scope['method'] in {'POST', 'PUT', 'PATCH'}:
            body = bytearray()
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                body.extend(message.get('body', b''))
                if len(body) > limit:
                    return await error_response(request_id, 'REQUEST_TOO_LARGE', 413)(scope, receive, send)
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
                message['headers'] = [*message.get('headers', []), (b'x-request-id', request_id.encode())]
            await send(message)

        await self.app(scope, receive, tagged_send)


def create_app(settings: Settings | None = None, *, llm_client=None, recipe_llm=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        effective = settings or Settings.from_env()
        application.state.settings = effective
        store = FileRunStore(
            effective.data_dir,
            secret_values=(
                effective.service_token.get_secret_value(),
                effective.agent_config['llm'].get('api_key', ''),
                read_secret('API_TOKEN'),
            ),
        )
        agent = AgentService(effective.agent_config, locks=store.locks, llm_client=llm_client)
        runtime = RunService(store, agent, max_runs=effective.max_runs, queue_limit=effective.queue_limit)
        application.state.runs = runtime
        builder = None
        try:
            await runtime.initialize()
            builder = RecipeService(
                runtime,
                recipe_llm
                or HTTPLLMClient(
                    effective.agent_config['llm'],
                    agent._http_session,
                ),
                timeout=effective.recipe_timeout,
                concurrency=effective.recipe_concurrency,
            )
            application.state.recipes = builder
            yield
        finally:
            runtime.accepting = False
            if runtime._operations:
                import asyncio

                await asyncio.gather(*runtime._operations, return_exceptions=True)
            if builder is not None:
                await builder.close()
            await runtime.close()

    application = FastAPI(title='MS-Agent AIGC Service', version='1.0.0', lifespan=lifespan)
    application.add_middleware(RequestBoundary, application=application)
    application.include_router(runs.router, prefix='/v1/aigc')
    application.include_router(recipes.router, prefix='/v1/aigc')

    @application.exception_handler(RunError)
    async def run_error(request: Request, exc: RunError):
        return error_response(request.state.request_id, exc.code, exc.status, exc.details)

    @application.exception_handler(PersistenceError)
    async def storage_error(request: Request, exc: PersistenceError):
        return error_response(request.state.request_id, 'STORAGE_UNAVAILABLE', 503)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [{'location': list(error['loc']), 'type': error['type']} for error in exc.errors()]
        return error_response(request.state.request_id, 'INPUT_INVALID', 422, details)

    @application.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError):
        return error_response(request.state.request_id, 'INPUT_INVALID', 400)

    @application.get('/health/live', tags=['Health'])
    async def live():
        return {'status': 'live'}

    @application.get('/health/ready', tags=['Health'])
    async def ready():
        runtime = application.state.runs
        healthy = runtime.accepting and runtime.store.healthy and runtime.store.lease._fd is not None
        return JSONResponse({'status': 'ready' if healthy else 'unavailable'}, status_code=200 if healthy else 503)

    return application
