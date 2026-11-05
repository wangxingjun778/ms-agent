"""对外 API 输入/输出结构；所有身份由认证依赖注入。"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SafeID = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9_:@-][A-Za-z0-9_.:@-]{0,127}$')]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ImageReference(StrictModel):
    type: Literal['image'] = 'image'
    url: str = Field(min_length=1, max_length=10000)

    @model_validator(mode='after')
    def check_url(self):
        if not self.url.startswith(('http://', 'https://')):
            raise ValueError('图片只接受可由外部 API 读取的 HTTP/HTTPS URL')
        return self


class InputMessage(StrictModel):
    role: Literal['user'] = 'user'
    content: str = Field(min_length=1, max_length=20000)
    attachments: list[ImageReference] = Field(default_factory=list, max_length=4)


class RunParameters(StrictModel):
    max_rounds: int | None = Field(default=None, ge=1, le=100)
    system_prompt: str | None = Field(default=None, max_length=20000)
    temperature: float | None = Field(default=None, ge=0, le=2)


class StartRunRequest(StrictModel):
    run_uuid: str
    session_id: SafeID
    profile_id: Literal['aigc_assistant'] = 'aigc_assistant'
    messages: list[InputMessage] = Field(min_length=1, max_length=20)
    parameters: RunParameters = Field(default_factory=RunParameters)

    @model_validator(mode='after')
    def normalize_uuid(self):
        self.run_uuid = str(UUID(self.run_uuid))
        return self

    def sdk_input(self) -> dict[str, Any]:
        data = self.model_dump()
        data['parameters'] = self.parameters.model_dump(exclude_none=True)
        return data


class ControlRunRequest(StrictModel):
    command_id: SafeID
    type: Literal['approve', 'skip', 'input', 'intervene', 'stop']
    interaction_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_payload(self):
        if self.type in {'approve', 'skip', 'input'} and not self.interaction_id:
            raise ValueError('该命令需要 interaction_id')
        if self.type == 'intervene':
            if set(self.payload) != {'content'} or not isinstance(self.payload['content'], str):
                raise ValueError('intervene 需要唯一的 content 字符串')
            if not 1 <= len(self.payload['content']) <= 20000:
                raise ValueError('干预内容长度无效')
        if self.type in {'approve', 'skip', 'stop'} and self.payload:
            raise ValueError('该命令不能附带 payload')
        if self.type in {'stop', 'intervene'} and self.interaction_id is not None:
            raise ValueError('该命令不使用 interaction_id')
        return self


class RecipeBuildRequest(StrictModel):
    recipe_request_id: SafeID
    artifact_ids: list[str] = Field(min_length=1, max_length=100)
    context: str = Field(default='', max_length=20000)

    @model_validator(mode='after')
    def normalize_selection(self):
        self.artifact_ids = sorted(set(self.artifact_ids))
        return self


class CommandReceipt(StrictModel):
    command_id: str
    type: str
    digest: str
    status: Literal['accepted', 'applied', 'rejected', 'no_op']
    reason_code: str
    applied_agent_seq: int | None = None
    remote_cancel_supported: bool | None = None
    remote_task_may_continue: bool | None = None


class RunView(StrictModel):
    run_uuid: str
    session_id: str | None = None
    profile_id: str | None = None
    status: Literal[
        'missing', 'queued', 'running', 'waiting_input', 'stopping', 'succeeded', 'failed', 'cancelled', 'interrupted'
    ]
    request_digest: str | None = None
    snapshot_digest: str | None = None
    last_agent_seq: int = 0
    pending_interaction: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    external_tasks: list[dict[str, Any]] = Field(default_factory=list)
    commands: dict[str, CommandReceipt] = Field(default_factory=dict)
    requires_reconciliation: bool = False
    created_at: str
    updated_at: str
    links: dict[str, str] = Field(default_factory=dict)


class EventEnvelope(StrictModel):
    schema_version: int
    run_uuid: str
    agent_seq: int
    event_type: str
    timestamp: str
    data: dict[str, Any]


class Recipe(StrictModel):
    recipe_uuid: str
    version: int
    source: dict[str, Any]
    prompt_template: str
    parameters: dict[str, Any]
    variables: list[dict[str, Any]]
    dependencies: list[dict[str, Any]]
    examples: list[dict[str, Any]]
    artifact_refs: list[dict[str, Any]]
    provenance: dict[str, Any]


class RecipeView(StrictModel):
    recipe_request_id: str
    recipe_uuid: str
    run_uuid: str
    status: Literal['queued', 'running', 'succeeded', 'failed', 'interrupted']
    result: Recipe | None = None
    error: dict[str, Any] | None = None
    created_at: str
    updated_at: str


class ErrorResponse(StrictModel):
    code: str
    message: str
    request_id: str
    details: Any = None
