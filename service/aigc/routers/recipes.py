"""独立配方构建台账的 HTTP 适配。"""

from uuid import UUID

from fastapi import APIRouter, Request, Response

from service.aigc.dependencies import User
from service.aigc.schemas import ErrorResponse, RecipeBuildRequest, RecipeView

router = APIRouter(prefix='/runs', tags=['Recipes'])


@router.post(
    '/{run_uuid}/recipe',
    response_model=RecipeView,
    operation_id='GetOrBuildRecipe',
    responses={code: {'model': ErrorResponse} for code in (401, 404, 409, 422, 429, 503)},
)
async def get_or_build_recipe(
    run_uuid: UUID,
    body: RecipeBuildRequest,
    request: Request,
    response: Response,
    user: User,
) -> dict:
    view, _ = await request.app.state.recipes.get_or_build(user, str(run_uuid), body.model_dump())
    response.status_code = 202 if view['status'] in {'queued', 'running'} else 200
    return view
