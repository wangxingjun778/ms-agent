"""Agentic 联调接口的输入与公开事实类型；不包含接口框架、凭据或业务数据库。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

# 版本变更必须先获得用户审批，联调功能迭代不自动升版。
PROTOCOL_VERSION = 1
SERVICE_VERSION = '1.0.0'
API_PREFIX = '/v1/aigc'
SafeID = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9_:@-][A-Za-z0-9_.:@-]{0,127}$')]
UUIDText = Annotated[
    str, StringConstraints(min_length=36, max_length=36), AfterValidator(lambda value: str(UUID(value)))
]
RunStatus = Literal[
    'missing',
    'queued',
    'running',
    'waiting_input',
    'stopping',
    'interrupted',
    'succeeded',
    'partial_succeeded',
    'failed',
    'cancelled',
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class OutputRef(StrictModel):
    assistant_message_uuid: UUIDText
    output_id: SafeID


class MediaInput(StrictModel):
    input_id: SafeID
    resource_ref: str = Field(min_length=1, max_length=1024)
    type: Literal['image', 'video'] = 'image'
    url: str = Field(min_length=1, max_length=10000)
    media_type: str | None = None
    sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    duration_seconds: float | None = Field(default=None, gt=0)
    display_name: str | None = None

    @model_validator(mode='after')
    def validate_url(self):
        from urllib.parse import urlsplit

        url = urlsplit(self.url)
        if url.scheme not in ('http', 'https') or not url.netloc or url.username:
            raise ValueError('素材 URL 必须为不含凭据的 HTTP/HTTPS 地址')
        return self


class ContentBlock(StrictModel):
    type: Literal['text'] = 'text'
    text: str = Field(max_length=20000)


class ContextMessage(StrictModel):
    message_uuid: UUIDText
    role: Literal['user', 'assistant']
    content: list[ContentBlock] = Field(default_factory=list, max_length=20)
    output_refs: list[OutputRef] = Field(default_factory=list, max_length=100)


class LoRA(StrictModel):
    model_ref: str = Field(min_length=1, max_length=1024)
    weight: float


class Preferences(StrictModel):
    aspect_ratio: str | None = None
    image_count: int = Field(default=1, ge=1, le=4)
    video_duration: int | None = Field(default=None, gt=0)
    resolution: str | None = None
    seed: int | None = None
    auto_evaluate: bool = False
    enabled_model_refs: list[str] = Field(default_factory=list, max_length=100)


class CustomSelection(StrictModel):
    model_ref: str = Field(min_length=1, max_length=1024)
    loras: list[LoRA] = Field(default_factory=list, max_length=6)
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def seed_location(self):
        if 'seed' in self.params:
            raise ValueError('seed 只允许在 effective.preferences.seed 中定义')
        if {
            'model',
            'model_ref',
            'prompt',
            'negative_prompt',
            'image_count',
            'video_duration',
            'aspect_ratio',
            'resolution',
            'input_refs',
            'loras',
        } & self.params.keys():
            raise ValueError('Custom params 不能重复定义 effective 或 LoRA 字段')
        if len({item.model_ref for item in self.loras}) != len(self.loras):
            raise ValueError('LoRA 模型引用不能重复')
        return self


class EffectiveInput(StrictModel):
    generation_mode: Literal['agent', 'custom'] = 'agent'
    model_source: Literal['community', 'byok'] = 'community'
    byok_connection_id: SafeID | None = None
    modality: Literal['image', 'video'] = 'image'
    action: Literal['generate', 'continue_edit', 'regenerate', 'change_reference', 'rethink'] = 'generate'
    prompt: str = Field(min_length=1, max_length=20000)
    negative_prompt: str = Field(default='', max_length=4000)
    preferences: Preferences = Field(default_factory=Preferences)
    custom_selection: CustomSelection | None = None

    @model_validator(mode='after')
    def modes(self):
        if (self.generation_mode == 'custom') != (self.custom_selection is not None):
            raise ValueError('Custom 必须有 custom_selection，Agent 不接受该字段')
        if (self.model_source == 'byok') != (self.byok_connection_id is not None):
            raise ValueError('BYOK 必须提供连接引用，community 不接受该字段')
        if self.action == 'rethink' and self.generation_mode != 'agent':
            raise ValueError('rethink 只适用于 Agent')
        return self


class ExplicitRequest(StrictModel):
    """原始显式意图用于追溯，绝不用于覆盖 effective。"""

    client_request_id: SafeID | None = None
    generation_mode: Literal['agent', 'custom'] | None = None
    model_source: Literal['community', 'byok'] | None = None
    modality: Literal['image', 'video'] | None = None
    action: Literal['generate', 'continue_edit', 'regenerate', 'change_reference', 'rethink', 'retry'] | None = None
    prompt: str | None = Field(default=None, max_length=20000)
    negative_prompt: str | None = Field(default=None, max_length=4000)
    conversation_uuid: UUIDText | None = None
    parent_message_uuid: UUIDText | None = None
    byok_connection_id: SafeID | None = None
    source_run_uuid: UUIDText | None = None
    source_output_refs: list[OutputRef] | None = None
    preferences: Preferences | None = None
    custom_selection: CustomSelection | None = None
    asset_refs: list['AssetRef'] | None = None
    entry_context: 'EntryContext | None' = None
    input_refs: list[dict[str, str]] | None = None


class AssetRef(StrictModel):
    type: Literal['skill', 'recipe']
    resource_ref: str = Field(min_length=1, max_length=1024)
    revision: str = Field(min_length=1, max_length=128)
    variables: dict[str, Any] = Field(default_factory=dict)


class AssetSnapshot(StrictModel):
    type: Literal['skill', 'recipe']
    resource_ref: str
    revision: str
    content_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    content: dict[str, Any]


class SourceOutput(StrictModel):
    output_ref: OutputRef
    type: Literal['image', 'video']
    url: str
    poster_url: str | None = None
    actual_generation: dict[str, Any] | None = None


class EntryContext(StrictModel):
    source: Literal['chat', 'remix', 'tool_panel'] = 'chat'
    resource_ref: str | None = None


class InputSnapshot(StrictModel):
    schema_version: Literal[1] = PROTOCOL_VERSION
    request: ExplicitRequest = Field(default_factory=ExplicitRequest)
    effective: EffectiveInput
    context_mode: Literal['explicit_snapshot'] = 'explicit_snapshot'
    context_policy: Literal['preserve'] = 'preserve'
    context: list[ContextMessage] = Field(default_factory=list, max_length=200)
    source_outputs: list[SourceOutput] = Field(default_factory=list, max_length=100)
    input_refs: list[MediaInput] = Field(default_factory=list, max_length=100)
    asset_refs: list[AssetRef] = Field(default_factory=list, max_length=20)
    asset_snapshots: list[AssetSnapshot] = Field(default_factory=list, max_length=20)
    memory_snapshot: list[str] = Field(default_factory=list, max_length=20)
    entry_context: EntryContext = Field(default_factory=EntryContext)
    provenance: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode='after')
    def references(self):
        ids = [m.message_uuid for m in self.context]
        if len(set(ids)) != len(ids):
            raise ValueError('context 消息标识不能重复')
        for message in self.context:
            UUID(message.message_uuid)
        if len({m.input_id for m in self.input_refs}) != len(self.input_refs):
            raise ValueError('input_id 不能重复')
        if self.effective.action != 'generate' and not self.source_outputs:
            raise ValueError('结果操作必须提供来源产物')
        if self.effective.action in ('continue_edit', 'change_reference') and not self.input_refs:
            raise ValueError('修改或换参考必须给出解析后的完整输入集合')

        # 开放 JSON 仍不可夹带凭据、连接地址或任意代码。
        def check(value):
            if isinstance(value, dict):
                forbidden = {
                    'api_key',
                    'token',
                    'authorization',
                    'password',
                    'credentials',
                    'base_url',
                    'code',
                    'scripts',
                }
                if forbidden & {str(k).lower() for k in value}:
                    raise ValueError('快照禁止携带凭据、连接地址或脚本')
                for item in value.values():
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)

        check(self.model_dump())
        json.dumps(self.model_dump(), allow_nan=False)
        return self


class ExecutionPolicy(StrictModel):
    deadline_at: str

    @model_validator(mode='after')
    def deadline(self):
        deadline = datetime.fromisoformat(self.deadline_at)
        if deadline.tzinfo is None:
            raise ValueError('deadline_at 必须含时区')
        self.deadline_at = deadline.astimezone(timezone.utc).isoformat()
        return self


class StartRunRequest(StrictModel):
    schema_version: Literal[1] = PROTOCOL_VERSION
    run_uuid: UUIDText
    session_id: UUIDText
    user_message_uuid: UUIDText
    assistant_message_uuid: UUIDText
    parent_message_uuid: UUIDText
    backend_request_hash: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    source_run_uuid: UUIDText | None = None
    execution_policy: ExecutionPolicy
    input_snapshot: InputSnapshot

    @model_validator(mode='after')
    def associations(self):
        for name in (
            'run_uuid',
            'session_id',
            'user_message_uuid',
            'assistant_message_uuid',
            'parent_message_uuid',
            'source_run_uuid',
        ):
            value = getattr(self, name)
            if value is not None:
                setattr(self, name, str(UUID(value)))
        if len({self.user_message_uuid, self.assistant_message_uuid, self.parent_message_uuid}) != 3:
            raise ValueError('用户/助手消息及父节点必须不同')
        if any(
            message.message_uuid in (self.user_message_uuid, self.assistant_message_uuid)
            for message in self.input_snapshot.context
        ):
            raise ValueError('祖先链不能包含本轮消息')
        if self.input_snapshot.context and self.input_snapshot.context[-1].message_uuid != self.parent_message_uuid:
            raise ValueError('context 末尾必须是指定父节点')
        return self

    def sdk_input(self):
        return self.model_dump(mode='json')


class InteractionInput(StrictModel):
    text: str | None = Field(default=None, max_length=20000)
    values: dict[str, Any] = Field(default_factory=dict)
    input_refs: list[MediaInput] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def no_credentials(self):
        def check(value):
            if isinstance(value, dict):
                if {'api_key', 'token', 'credentials', 'authorization', 'password'} & {str(k).lower() for k in value}:
                    raise ValueError('交互输入不接受凭据')
                for item in value.values():
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)

        check(self.values)
        return self


class ControlRunRequest(StrictModel):
    command_id: SafeID
    action: Literal['approve', 'skip', 'submit_input', 'intervene', 'stop']
    interaction_id: SafeID | None = None
    action_id: SafeID | None = None
    input: InteractionInput = Field(default_factory=InteractionInput)
    stop_reason: Literal['USER_STOP', 'DEADLINE'] | None = None

    @model_validator(mode='after')
    def target(self):
        if self.action == 'stop':
            if self.interaction_id or self.action_id or self.input.text or self.input.values or self.input.input_refs:
                raise ValueError('stop 不能包含交互目标或输入')
            self.stop_reason = self.stop_reason or 'USER_STOP'
        elif not self.interaction_id or not self.action_id or self.stop_reason is not None:
            raise ValueError('非 stop 必须绑定 interaction_id/action_id 且不含 stop_reason')
        if self.action in ('approve', 'skip') and (self.input.text or self.input.values or self.input.input_refs):
            raise ValueError('授权/跳过不接受额外输入')
        return self


class RecipeBuildRequest(StrictModel):
    recipe_request_id: SafeID
    output_ids: list[SafeID] = Field(min_length=1, max_length=100)
    context: str = Field(default='', max_length=20000)
    retry_command_id: SafeID | None = None
    expected_attempt: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def normalize(self):
        self.output_ids = sorted(set(self.output_ids))
        if (self.retry_command_id is None) != (self.expected_attempt is None):
            raise ValueError('显式重试需要 retry_command_id 和 expected_attempt')
        return self


class PublicError(StrictModel):
    code: str
    message: str


class GenerationFacts(StrictModel):
    requested: dict[str, Any] = Field(default_factory=dict)
    submitted: dict[str, Any] = Field(default_factory=dict)
    confirmed: dict[str, Any] | None = None


class ResultAction(StrictModel):
    action: Literal['continue_edit', 'regenerate', 'change_reference', 'rethink', 'save_recipe']
    label: str
    output_refs: list[OutputRef]
    suggestions: list[str] = Field(default_factory=list)


class Output(StrictModel):
    output_id: SafeID
    type: Literal['text', 'image', 'video']
    content: str | None = None
    url: str | None = None
    poster_url: str | None = None
    reference_inputs: list[MediaInput] = Field(default_factory=list)
    asset_refs: list[AssetRef] = Field(default_factory=list)
    generation: GenerationFacts = Field(default_factory=GenerationFacts)
    actual_generation: dict[str, Any] | None = None
    result_actions: list[ResultAction] = Field(default_factory=list)


class InteractionAction(StrictModel):
    action_id: SafeID
    action: Literal['approve', 'skip', 'submit_input', 'intervene']
    label: str = Field(max_length=10)
    input_schema: dict[str, Any] | None = None


class Interaction(StrictModel):
    interaction_id: SafeID
    type: Literal['permission', 'input', 'intervention']
    title: str
    blocking: bool
    step_id: str | None = None
    arguments: dict[str, Any] | None = None
    actions: list[InteractionAction] = Field(max_length=4)


class CommandReceipt(StrictModel):
    run_uuid: str
    command_id: str
    action: str
    command_status: Literal['accepted', 'applied', 'rejected', 'no_op']
    reason_code: str
    interaction_id: str | None = None
    action_id: str | None = None
    applied_agent_seq: int | None = None
    state_revision: int = 0
    run_status: RunStatus
    execution_closed: bool
    settlement_status: Literal['none', 'tracking', 'resolved', 'unknown']
    can_release_activity: bool
    remote_cancel_supported: bool = False
    remote_task_may_continue: bool = False


class Step(StrictModel):
    step_id: str
    title: str
    status: Literal['running', 'succeeded', 'failed', 'cancelled', 'interrupted']
    summary: str = ''
    tool: dict[str, str] | None = None
    error: PublicError | None = None


class Summary(StrictModel):
    summary_id: str
    step_id: str | None = None
    text: str


class CompletionItem(StrictModel):
    item_id: str
    output_ids: list[str] = Field(default_factory=list)
    reason_code: str | None = None
    message: str | None = None


class Completion(StrictModel):
    completed_items: list[CompletionItem] = Field(default_factory=list)
    incomplete_items: list[CompletionItem] = Field(default_factory=list)


class Settlement(StrictModel):
    status: Literal['none', 'tracking', 'resolved', 'unknown'] = 'none'
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    error: PublicError | None = None


class PublicSnapshot(StrictModel):
    schema_version: Literal[1] = PROTOCOL_VERSION
    run_uuid: str
    session_id: str | None = None
    user_message_uuid: str | None = None
    assistant_message_uuid: str | None = None
    parent_message_uuid: str | None = None
    status: RunStatus
    state_revision: int
    snapshot_agent_seq: int
    behavior_preview: dict[str, Any] | None = None
    steps: list[Step] = Field(default_factory=list)
    reasoning_summaries: list[Summary] = Field(default_factory=list)
    reply: str = ''
    outputs: list[Output] = Field(default_factory=list)
    result_actions: list[ResultAction] = Field(default_factory=list)
    pending_interactions: list[Interaction] = Field(default_factory=list)
    commands: list[CommandReceipt] = Field(default_factory=list)
    completion: Completion = Field(default_factory=Completion)
    settlement: Settlement = Field(default_factory=Settlement)
    execution_closed: bool = False
    can_release_activity: bool = False
    error: PublicError | None = None


class RunView(StrictModel):
    schema_version: Literal[1] = PROTOCOL_VERSION
    run_uuid: str
    agent_run_id: str
    session_id: str | None = None
    user_message_uuid: str | None = None
    assistant_message_uuid: str | None = None
    parent_message_uuid: str | None = None
    status: RunStatus
    state_revision: int
    last_agent_seq: int
    snapshot_agent_seq: int
    request_digest: str | None = None
    input_snapshot_digest: str | None = None
    execution_snapshot_digest: str | None = None
    public_snapshot: PublicSnapshot
    completion: Completion
    settlement: Settlement
    execution_closed: bool
    can_release_activity: bool
    created_at: str
    updated_at: str
    completed_at: str | None = None
    error: PublicError | None = None
    links: dict[str, str] = Field(default_factory=dict)


class EventEnvelope(StrictModel):
    schema_version: Literal[1] = PROTOCOL_VERSION
    run_uuid: str
    agent_seq: int
    event_type: str
    timestamp: str
    payload: dict[str, Any]


class Recipe(StrictModel):
    schema_version: Literal[1] = PROTOCOL_VERSION
    recipe_uuid: str
    content_revision: int = 1
    source: dict[str, Any]
    prompt_template: str
    negative_prompt_template: str
    parameters: dict[str, Any]
    variables: list[dict[str, Any]]
    dependencies: list[dict[str, str]]
    examples: list[dict[str, Any]]
    artifact_refs: list[Output]
    provenance: dict[str, str]


class RecipeView(StrictModel):
    schema_version: Literal[1] = PROTOCOL_VERSION
    recipe_request_id: str
    recipe_uuid: str
    run_uuid: str
    attempt: int
    status: Literal['queued', 'running', 'succeeded', 'failed', 'interrupted']
    input_digest: str
    result: Recipe | None = None
    error: PublicError | None = None
    retry_receipts: list[dict[str, Any]] = Field(default_factory=list)
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str
    updated_at: str


class ErrorResponse(StrictModel):
    code: str
    message: str
    request_id: str
    details: Any = None
