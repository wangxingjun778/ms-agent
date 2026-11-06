"""五接口统一使用 SDK 定义的 Agentic 联调类型，拒绝弃用字段。"""

from ms_agent.agent.protocol import (
    CommandReceipt,
    ControlRunRequest,
    ErrorResponse,
    EventEnvelope,
    Recipe,
    RecipeBuildRequest,
    RecipeView,
    RunView,
    StartRunRequest,
)

__all__ = [
    'CommandReceipt',
    'ControlRunRequest',
    'ErrorResponse',
    'EventEnvelope',
    'Recipe',
    'RecipeBuildRequest',
    'RecipeView',
    'RunView',
    'StartRunRequest',
]
