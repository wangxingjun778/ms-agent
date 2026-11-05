"""Run 的 REST 与 SSE 适配，执行逻辑均委托 SDK。"""

from __future__ import annotations

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from ms_agent.agent.run_service import RunError
from ms_agent.session.locking import PersistenceError
from service.aigc.dependencies import Runs, User
from service.aigc.schemas import (
    CommandReceipt,
    ControlRunRequest,
    ErrorResponse,
    EventEnvelope,
    RunView,
    StartRunRequest,
)

router = APIRouter(prefix='/runs', tags=['Runs'])
_ERRORS = {code: {'model': ErrorResponse} for code in (400, 401, 404, 409, 422, 429, 503)}


def with_links(view: dict) -> dict:
    prefix = f'/v1/aigc/runs/{view["run_uuid"]}'
    return {
        **view,
        'links': {
            'self': prefix,
            'events': prefix + '/events',
            'commands': prefix + '/commands',
            'recipe': prefix + '/recipe',
        },
    }


@router.post('', response_model=RunView, operation_id='StartRun', status_code=202, responses=_ERRORS)
async def start_run(body: StartRunRequest, response: Response, user: User, runs: Runs) -> dict:
    view, created = await runs.start(user, body.sdk_input())
    response.status_code = 202 if created and view['status'] == 'queued' else 200
    return with_links(view)


@router.get('/{run_uuid}', response_model=RunView, operation_id='GetRun', responses=_ERRORS)
async def get_run(run_uuid: UUID, user: User, runs: Runs) -> dict:
    return with_links(await runs.get(user, str(run_uuid)))


@router.post('/{run_uuid}/commands', response_model=CommandReceipt, operation_id='ControlRun', responses=_ERRORS)
async def control_run(run_uuid: UUID, body: ControlRunRequest, response: Response, user: User, runs: Runs) -> dict:
    receipt = await runs.command(user, str(run_uuid), body.model_dump())
    if receipt['status'] == 'rejected':
        raise RunError(receipt['reason_code'], 409, receipt)
    response.status_code = 202 if receipt['status'] == 'accepted' else 200
    return receipt


@router.get(
    '/{run_uuid}/events',
    operation_id='SubscribeRunEvents',
    response_class=StreamingResponse,
    responses={
        **_ERRORS,
        200: {
            'content': {'text/event-stream': {'schema': {'type': 'string'}}},
            'description': 'SSE data 为 EventEnvelope，id 为 agent_seq',
        },
    },
    openapi_extra={'x-event-schema': EventEnvelope.model_json_schema()},
)
async def subscribe_run_events(
    run_uuid: UUID,
    request: Request,
    user: User,
    runs: Runs,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header(alias='Last-Event-ID')] = None,
) -> StreamingResponse:
    try:
        cursor = after_seq if after_seq is not None else int(last_event_id or '0')
    except ValueError:
        raise RunError('EVENT_CURSOR_INVALID', 400) from None
    view = await runs.get(user, str(run_uuid))
    if not 0 <= cursor <= view['last_agent_seq']:
        raise RunError('EVENT_CURSOR_INVALID', 400)

    async def stream():
        try:
            async for event in runs.events(user, str(run_uuid), cursor):
                if await request.is_disconnected():
                    break
                if event is None:
                    yield ': heartbeat\n\n'
                else:
                    yield f'id: {event["agent_seq"]}\nevent: {event["event_type"]}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n'
        except PersistenceError:
            # 已发送响应头后不可伪造成功终态；关闭连接让调用方重连查询。
            return

    return StreamingResponse(
        stream(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )
