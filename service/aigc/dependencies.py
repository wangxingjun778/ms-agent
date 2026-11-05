"""可信 backend 的服务间认证与身份传递。"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ms_agent.agent.run_service import RunError, RunService
from ms_agent.session.locking import validate_id

_bearer = HTTPBearer(auto_error=False)


async def authenticated_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    user_id: Annotated[str | None, Header(alias='X-User-Id')] = None,
) -> str:
    settings = request.app.state.settings
    token = settings.credentials.service.get_secret_value()
    value = request.headers.get(settings.auth.header, '')
    if settings.auth.scheme == 'bearer':
        scheme, _, value = value.partition(' ')
        if scheme.lower() != 'bearer':
            raise RunError('UNAUTHORIZED', 401)
    if not token or not secrets.compare_digest(value.encode(), token.encode()):
        raise RunError('UNAUTHORIZED', 401)
    try:
        return validate_id(user_id, 'user_id')
    except ValueError:
        raise RunError('USER_ID_INVALID', 400) from None


User = Annotated[str, Depends(authenticated_user)]


def run_service(request: Request) -> RunService:
    return request.app.state.runs


Runs = Annotated[RunService, Depends(run_service)]
