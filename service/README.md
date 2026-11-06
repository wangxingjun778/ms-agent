# AIGC Agentic 服务接口说明

> 接口前缀：`/v1/aigc` · 服务声明：`1.0.0` · 文档修订日期：`2026-11-06`。
> 当前处于联调阶段，尚未部署上线。本文按最新工作区实现描述，保留已完成的 Agentic 能力，不因功能迭代自动升级接口版本。任何接口版本变更必须先获得用户审批；版本约定与变更记录见第 10 节。

## 1. 链路、职责与交付范围

```text
前端登录态 / 用户操作
  → modelscope_service（业务 DB 事务、消息树、快照、后台租约、事件投影）
  → EAS 网关 → service/aigc FastAPI 服务
  → ms_agent SDK（Agent/Custom、工具调用、持久事实、控制、收尾、Recipe）
  → 已配置模型 / aigc_service HTTP API
```

后端先在 DB 中创建 Run 和双消息，再由持久后台待办调用 ms-agent；后端的 202 与本服务的 202 分别表示各层受理，不代表模型开始或完成。后端负责会话归属、消息树、资源授权、偏好合并、前端版本及 Skill 管理；本服务负责执行和原任务查询，不创建后端消息或推理平台 task 表。

当前已实现的业务接口如下：

| 接口 | 方法及服务内路径 | 用途 |
|---|---|---|
| StartRun | POST `/v1/aigc/runs` | 固定 Run 与快照的持久幂等受理 |
| GetRun | GET `/v1/aigc/runs/{run_uuid}` | 查询同水位完整公共快照、命令与收尾状态 |
| SubscribeRunEvents | GET `/v1/aigc/runs/{run_uuid}/events` | Agent 事件按序回放与持续订阅 |
| ControlRun | POST `/v1/aigc/runs/{run_uuid}/commands` | 精确交互及停止门闩 |
| GetOrBuildRecipe | POST `/v1/aigc/runs/{run_uuid}/recipe` | 固定事实构建、查询及显式 attempt 重试 |

`/health/live`、`/health/ready` 与 `/internal/stats` 是运维端点，与上述业务接口分开。`/docs`、`/openapi.json` 提供类型约束。SDK 类型源为 [protocol.py](../ms_agent/agent/protocol.py)。

### 1.1 当前能力与依赖限制

- 默认开放 `Qwen/Qwen-Image-2.1-Turbo` 的已配置图片工具。Agent 可返回纯文本或调用工具；Custom 直接调用固定工具，不调用规划 LLM。
- 仅使用明确给出的显式祖先链、冻结 memory 和声明式资产；不自动拼接 session 历史、不读取最新用户偏好、不裁剪或自动摘要。
- 视频输出/封面/confirmed 参数已具备配置适配及 mock 契约测试，但默认没有生产视频工具，不能宣称真实视频服务已接通。
- BYOK 的 `ConnectionResolver` 接入边界已定义，当前配置为 disabled，调用明确拒绝。没有用户凭据解析服务的实际接口契约，不从连接 ID 猜测凭据或地址。
- Skill/Recipe 通过可信 backend 提供的固定 `asset_snapshots` 解析，验证 revision/digest/变量；不从任意用户 URL 下载代码，不安装 Skill，不支持 MCP。
- 原任务收尾只使用已配置 `poll_path` 查询 task_id。当前没有远端 cancel 或 invocation_id 查询接口，`request_id` 只用于追踪，不是任务查询键。
- 本服务没有配方编辑、Skill 包构建或发布接口。后端确定性打包与调用 Skill 中心；需同步调整后端 V3 5.1.6 与划掉 BuildSkillPackage 的矛盾。

## 2. 公共约定

### 2.1 认证与地址

本地默认 `Authorization: Bearer <service-token>`；应用凭据格式由 `auth.header/auth.scheme` 唯一决定。EAS 生成配置采用：

```text
Authorization: <EAS_TOKEN>                  # 网关鉴权
X-AIGC-Service-Token: <service-token>       # FastAPI 服务间鉴权
X-User-Id: <可信 backend 校验后的用户>
Content-Type: application/json
```

不要向浏览器暴露服务凭据；本服务信任持有凭据的 backend 注入用户标识，不验证站点登录态。查询他人 Run 返回 404。

EAS 的 `<EAS_ENDPOINT>` 已含 `/api/predict/<service_name>`，调用时追加 `/v1/aigc/...`。响应 links 是含 `public_path_prefix` 的站点根相对路径，不重复添加 EAS 前缀。实际联调请求按上述配置传递鉴权头，不能混用网关 token 与应用 token。

### 2.2 标识与版本

- run_uuid、session_id、双消息 UUID、parent_message_uuid 均为合法 UUID；session_id 对应 backend conversation_uuid。后端选择和验证父链。
- agent_run_id 固定等于 run_uuid，不产生第二套执行关联。
- command_id、recipe_request_id、retry_command_id 是 1～128 字符的安全字符串。
- snapshot/envelope/Recipe schema_version 固定 1；不等于 Agent 事件序号、后端 version 或构建 attempt。
- state_revision 是本服务 journal 提交修订；后端 `version/applied_run_version` 仍由其 DB 事务产生。
- 服务为每次 HTTP 请求生成追踪标识并在响应 `X-Request-Id` 返回；当前不复用调用方传入的同名头，不参与业务幂等。
- `X-User-Id` 是可信后端提供的安全字符串，长度 1～128，允许字母、数字及 `_.:@-`，不接受 `.` 或 `..`；无效标识返回 400/USER_ID_INVALID。
- 严格请求拒绝未知字段和隐式数字/布尔转换，时间为含时区 ISO 8601。

### 2.3 摘要与幂等

| 摘要 | 覆盖范围 |
|---|---|
| request_digest | 规范化完整 StartRun envelope、关联 UUID、快照和执行 deadline |
| input_snapshot_digest | 冻结业务输入，保留 context/input_refs 数组顺序 |
| execution_snapshot_digest | 固定模型映射、配置、工具声明、资产与实际消息上下文，排除凭据 |
| backend_request_hash | 后端显式请求摘要，仅追踪，不要求与上述摘要相同 |
| config_digest | 服务配置修订，与代码版本及生成结果分开 |

Run 按 `(user_id, run_uuid)`；命令按 `(user_id, run_uuid, command_id)`；配方基础键按 `(user_id, recipe_request_id)` 幂等。同键异输入返回 409；网络重发复用原键，不重新执行或读取新资产版本。

业务主动 retry 使用新 run_uuid、新消息对和新 deadline，复用来源原 input_snapshot、session 和父节点。不得用新的 Run 隐藏未核清外部副作用；当前仅允许来源 failed/cancelled 且 can_release_activity=true 的 retry。

## 3. StartRun

### 3.1 请求字段

| 字段 | 必填 | 语义 |
|---|---|---|
| schema_version | 否 | 固定 1 |
| run_uuid/session_id | 是 | 共同执行 ID / 会话 UUID |
| user_message_uuid/assistant_message_uuid | 是 | backend 已创建的双消息，与彼此及父节点均不同；context 不能包含本轮消息 |
| parent_message_uuid | 是 | backend 选定分支的父节点；context 非空时末尾须与它一致 |
| backend_request_hash | 否 | SHA-256 追踪值 |
| source_run_uuid | 否 | 已核清失败/取消后的主动 retry 来源 |
| execution_policy.deadline_at | 是 | 本轮 deadline，不能沿用 retry 来源的已过期 deadline |
| input_snapshot | 是 | 下述 InputSnapshot |

`input_snapshot`：

| 字段 | 语义 |
|---|---|
| schema_version | 固定 1 |
| request | 规范化显式请求追溯；不再次合并，也不覆盖 effective |
| effective | 执行权威参数 |
| context_mode/context_policy | 固定 explicit_snapshot / preserve |
| context[] | 有序 user/assistant 消息，含 message_uuid、文本 content blocks、output_refs；不能重复 ID |
| input_refs[] | input_id、resource_ref、image/video 类型、稳定 URL、可选媒体 metadata；保持完整参考顺序 |
| source_outputs[] | 来源助手/产物引用、URL/封面和已知 actual_generation；未知保持 null |
| asset_refs[] | type=skill/recipe、resource_ref、固定 revision、variables |
| asset_snapshots[] | 同引用/revision、content_digest、声明式 content，必须完整对应 asset_refs |
| memory_snapshot[] | 明确冻结的参考文字，不加载动态 memory |
| entry_context/provenance | 入口和来源追溯，不改变参数优先级 |

`effective`：
- generation_mode=agent/custom；model_source=community/byok；modality=image/video。
- action=generate/continue_edit/regenerate/change_reference/rethink；主动 retry 复制原快照的生成 action，来源放顶层。
- prompt 必填；negative_prompt 默认空字符串。
- preferences：aspect_ratio、image_count（默认 1，1～4）、video_duration、resolution、seed、auto_evaluate、enabled_model_refs。Agent 未指定 enabled_model_refs 时只选服务配置中与当前 modality 匹配的模型；Custom 使用 custom_selection.model_ref。
- custom_selection：model_ref、loras[{model_ref,weight}]、params。Custom 必填，Agent 不接受；LoRA 引用不能重复，seed 只在 preferences.seed，params 不得重复定义 effective/素材/LoRA 字段。
- continue_edit/change_reference 必须有解析后的完整输入集合；结果操作必须有来源产物；regenerate 需要一个有已确认 prompt/model_ref 的来源。
- 模型、操作、参数、比例/清晰度组合必须有服务端已验证映射；无映射明确拒绝，不静默降级。默认模型支持映射 steps→steps、guidance_scale→guidance、seed、n 和图片参考；未开放 sampler/LoRA 映射，sizes 为空，因此比例/分辨率组合默认拒绝。
- 请求类型允许 prompt 最多 20,000 字符，但默认图片工具契约上限为 4,000；超过下游工具限制仍在能力校验阶段拒绝。默认最多 4 张图片参考、steps 1～50、guidance 1.5～20、seed -1～2147483647；以统一 YAML 中的工具 Schema 为最终执行范围。
- 当前未配置 auto_evaluate，不接受启用。视频只在配置明确模型/工具及结果映射后开放。

后端先完成“显式值＞允许继承值＞偏好＞冻结默认值”的字段合并。服务不二次读取偏好。`capabilities.context.max_chars` 统计最终序列化 LLM 消息（含 system prompt、effective、资产和 memory），不是只统计祖先文字；超预算返回 CONTEXT_TOO_LARGE，不自动摘要或截断。服务校验 UUID、唯一性和末尾父节点，但不访问后端 DB 验证完整祖先关系，父链授权与顺序仍由后端保证。

### 3.2 示例

```json
{
  "schema_version": 1,
  "run_uuid": "11111111-1111-4111-8111-111111111111",
  "session_id": "22222222-2222-4222-8222-222222222222",
  "user_message_uuid": "33333333-3333-4333-8333-333333333333",
  "assistant_message_uuid": "44444444-4444-4444-8444-444444444444",
  "parent_message_uuid": "55555555-5555-4555-8555-555555555555",
  "execution_policy": {"deadline_at": "2026-11-05T12:00:00+00:00"},
  "input_snapshot": {
    "schema_version": 1,
    "context_mode": "explicit_snapshot",
    "effective": {
      "generation_mode": "custom",
      "model_source": "community",
      "modality": "image",
      "action": "generate",
      "prompt": "生成一只猫",
      "preferences": {"image_count": 1, "seed": 42},
      "custom_selection": {"model_ref": "Qwen/Qwen-Image-2.1-Turbo"}
    },
    "context": [],
    "input_refs": [],
    "asset_refs": [],
    "asset_snapshots": []
  }
}
```

示例 deadline 需换成本轮真实时间；过去时间会生成截止停止事实，不执行工具。

### 3.3 响应与特性

首个请求 202 queued；原记录或取消墓碑 200。均返回 RunView：

```text
schema_version / run_uuid / agent_run_id / session_id / 双消息 / parent_message_uuid
status / state_revision / last_agent_seq / snapshot_agent_seq
request_digest / input_snapshot_digest / execution_snapshot_digest
public_snapshot / completion / settlement
execution_closed / can_release_activity
created_at / updated_at / completed_at / error / links
```

请求先查幂等记录，再进行首次能力校验/准入和固定 snapshot，最后创建后台执行；请求断开不撤销已受理事务。Custom 直接调用固定工具，不调用规划 LLM；Agent 工具输入受同一能力约束。按用户公平调度，同 session FIFO；后端“每会话一个未结束 Run”仍由其 active_slot 约束保证。

## 4. GetRun 与公共事实

GET 无请求体，200 返回 RunView；同 `If-None-Match` 返回 304，无正文。ETag 覆盖完整公共视图，包括回执/收尾变化，不能跨用户共享缓存。

`public_snapshot` 与 status、commands、settlement、snapshot_agent_seq 来自同一持久提交：

```text
schema_version / Run 关联 / state_revision / snapshot_agent_seq
behavior_preview {summary,tools,assets,actions}
steps[] {step_id,title,status,summary,tool,error}
reasoning_summaries[] {summary_id,step_id,text}
reply / outputs[] / result_actions[]
pending_interactions[] / commands[]
completion / settlement / execution_closed / can_release_activity / error
```

GetRun 不重新规划、不重提、不在 HTTP 请求内查询工具；后台查询的变化会持久化到后续水位。完整快照可覆盖原事件过程，backend 应用后从 snapshot_agent_seq 续订，并在自己的事务中生成 frontend version。

### 4.1 产物

Output 包含 output_id、type=text/image/video、content/url/poster_url、reference_inputs、asset_refs、generation、actual_generation、result_actions。

- generation.requested 是冻结意图，submitted 是已提交参数，confirmed 是下游明确返回的事实或 null。
- actual_generation 只来自 confirmed，不用 seed/model 请求值填未知实际值。
- configured confirmed_path/poster_path 指向可信下游响应字段；当前默认 ModelScope 配置没有 confirmed 参数和封面映射。
- 公共状态不包含工具 raw_data、system prompt、原始 reasoning、连接凭据或内部日志。
- output_id 稳定，按完整对象 upsert，不随排序变更。
- result_actions 依据产物和能力生成。只有 confirmed 带非空 prompt/model_ref 且模型允许 regenerate，才提供该动作；无确认实际参数时不会下发。图片修改/换参考还须有 input_refs 映射，rethink 仅用于 Agent 且模型允许；后端仍须过滤权限和业务状态。
- URL 直接返回，不下载/入库。内部 aigc_service 的永久性由其契约保证；默认远端 URL 不自动获得永久保证。

### 4.2 状态、完成度与活动位

| 状态 | 执行事实 |
|---|---|
| missing | StartRun 尚未绑定输入，可能已有控制账本 |
| queued/running | 排队 / 执行中 |
| waiting_input | 阻断式交互，归还计算槽但仍占业务在途容量 |
| stopping | 禁止新规划/提交，已知任务仍在收尾 |
| interrupted | 执行链中断或外部结果未核清；不能自动映射为 backend FAILED |
| succeeded/partial_succeeded | 有完成依据的完整 / 部分交付 |
| failed/cancelled | 已核清失败 / 用户停止 |

completion 含 completed_items[] 与 incomplete_items[{item_id,reason_code,message}]。图片/视频交付按冻结请求目标与确认产物核对；metadata.partial 和“有一张图”不是成功依据。纯文本回复可完成文本对话；当前没有开放复杂多目标规划或模型内部自动评估工具。

settlement.status=none/tracking/resolved/unknown，tasks 给出 invocation_id、tool_name、task_id、request_id 和状态，不含凭据或完整原始参数。

**后端只有看到 can_release_activity=true，并保存同水位完整结果后，才可以清空 active_slot/next_sync_at。** execution_closed 只表示本地不再规划/提交；控制 APPLIED、202、收到产物和 disconnected 都不是释放依据。

## 5. SubscribeRunEvents

游标优先级：after_seq＞Last-Event-ID＞0，只返回更高序号。无效游标 400；当前不自动删除事件，因此没有正常的历史过期窗口。未来启用保留水位时才能对真实过期返回 EVENT_CURSOR_EXPIRED，不能由连接 EOF 推断。

```text
id: 12
event: step.upsert
data: {"schema_version":1,"run_uuid":"...","agent_seq":12,"event_type":"step.upsert","timestamp":"...","payload":{"step_id":"...","title":"工具执行","status":"running","summary":"...","tool":{"type":"api","name":"image_generation"},"error":null,"run_state":{"state_revision":12}}}

: heartbeat
```

| 事件 | 消费语义 |
|---|---|
| run.status | 执行状态事实 |
| behavior.preview | 使用工具、资产及公共执行预告 |
| step.upsert | 按 step_id 替换完整步骤 |
| summary.delta | 按 summary_id 追加公共执行说明，不含私有推理 |
| reply.delta | 追加回复正文；模型非流式时可以一次返回完整段落 |
| output.upsert | 按 output_id 更新完整产物 |
| interaction.required/resolved | 有效交互 / RESOLVED 或 EXPIRED |
| command.result | accepted/applied/rejected/no_op 回执事实 |
| recipe.suggested | 建议保存事实配方，不自动构建或发布 |
| settlement.upsert | 收尾状态更新 |
| run.completed | 已提交最终完整水位，can_release_activity=true |

先 journal 提交和公共 reducer，再唤醒 SSE。每条 `payload.run_state` 带变化的公共生命周期字段及 state_revision；首次事件带完整关联，命令更新带公开 commands，snapshot_agent_seq 从 envelope.agent_seq 推导。消费 step/output/interaction 等完整对象时先去掉 run_state，再按该补丁更新顶层事实和回执的状态水位。这些补丁不包含内部 request/prepared/raw_data。

[共享 reducer](../ms_agent/agent/public_state.py) 支持 `reduce_public(previous_snapshot, None, event)`：仅用 SSE 即可独立重建完整公共快照；重复/已覆盖序号不会再追加 delta。backend 在每个 Run 的有序通路内，事务保存事件、消息、状态与水位；若过滤事件，序号允许跳号，但不能只凭高序号推断此前事件已被覆盖。完整 GetRun 快照可一次覆盖至 snapshot_agent_seq，之后只应用更高序号。

服务每批最多 `events.batch_size`（默认 128）条；网络发送在提交锁外。终态连接仍须发送该水位剩余全部批次，再关闭。

backend 的 applied_run_version 不由 ms-agent 生成；后端自产 `run.snapshot` 无 Agent id。服务错误可发送无 id 的 `stream.error` 控制帧后断开，不伪造终态。心跳不落盘不占序号，事件生成循环默认预算 540 秒、15 秒心跳，到时按最后成功游标重连；网络阻塞或正在发送的批次可能延后实际断流，不应把该值当硬性 TCP 关闭保证。慢连接网络写出不持有提交锁，不能阻塞工具；公开快照是追赶兜底。

## 6. ControlRun

POST 输入：

```json
{
  "command_id": "approve-001",
  "action": "approve",
  "interaction_id": "从当前交互读取",
  "action_id": "从该交互允许动作读取"
}
```

非 stop 必须携带两个目标 ID，action=approve/skip/submit_input/intervene。输入使用 `input.text/values/input_refs`，按 interaction.actions 的 input_schema 校验；不得携带凭据。stop 不携带交互/输入，仅可携带 stop_reason=USER_STOP/DEADLINE，默认 USER_STOP。

```json
{"command_id":"stop-001","action":"stop","stop_reason":"USER_STOP"}
```

交互最多一个阻断和一个非阻断干预入口；互斥动作只能一个成功。Agent 可通过 ask_input 在原 Run 内澄清，submit_input 返回后继续原轮，不创建新的 Run。工具授权只针对一次提交，Custom 不接受用户借补充输入暗中改写冻结参数。

回执：run_uuid、command_id、action、command_status、reason_code、目标 ID、applied_agent_seq、state_revision、run_status、execution_closed、settlement_status、can_release_activity、远端取消提示。

- accepted：202，尚未应用；applied/no_op：200；状态拒绝 409，交互输入无效 422。
- 重复命令先返回原回执，再检查当前交互；异输入 COMMAND_KEY_CONFLICT。
- 干预在下一个安全边界应用，只影响未执行的步骤；不取消已提交任务，不代替阻断授权。
- stop 立即持久设置门闩，阻止新提交；APPLIED 不意味着远端取消。首次停止原因固定，后续命令不覆盖。
- 未启动 stop 保存墓碑；迟到 StartRun 仍不执行。正常完成先持久提交则不改写终态。

### 6.1 工具与收尾边界

每次新副作用先持久化 submission intent，成功取得 task_id 后提交关联；批准后及取得实际 HTTP 调用槽后、POST 前再次校验门闩。在连接槽等待期间被停止属于已知未发送，可核清；已进入传输的超时/断连/5xx 保持未知，不能重提。远端轮询和收尾均不持有 Run 计算槽；收尾使用独立 `upstreams.settlement` 连接池、并发/RPM 限流，不占普通 poll 或 LLM 配额。

收尾预算耗尽返回 interrupted/unknown，保留原任务与活动位判据。重启只查询原任务，不续跑 agent；无法按 task_id 查询的未知提交持续告警，不用 request_id 猜测查询路径。当前不提供 cancel/lookup_submission transport，接口名称不代表已接通。

SIGTERM 停止受理、结束 SSE、立即取消本地执行和收尾监督器，并等待必要持久提交后释放租约；不等待远端任意长任务完成。`runtime.drain_timeout` 当前传入 close 并参与 EAS 退出预算校验，但 close 未用它实现“继续正常执行 N 秒”的等待，不能据此承诺按此时长平滑跑完。ASGI 优雅退出由 `server.graceful_shutdown_timeout` 控制，底层持久 IO 仍须等完成；EAS/NAS 故障与信号链路需真实验证，不承诺无损热更新。

## 7. GetOrBuildRecipe

```json
{"recipe_request_id":"recipe-001","output_ids":["final_text"],"context":"整理本次能力说明"}
```

首次构建 202；重复相同 POST 查询现状，queued/running 202，终态 200。来源必须同用户、同 Run 且 can_release_activity=true；不重读最新偏好/分支。选定 output_ids 排序去重，不接受旧 artifact_ids。

输出 RecipeView：schema_version=1、recipe_request_id、recipe_uuid、run_uuid、attempt、status、input_digest、result、error、retry_receipts、attempts、时间。

result：schema_version=1、content_revision=1、source、prompt_template、negative_prompt_template、parameters（submitted/confirmed）、variables{name,type,required,description,default?,target?}、dependencies{type,resource_ref,revision,content_digest}、examples、artifact_refs、provenance。

参数仅来自选定产物关联的提交事实；不把其他步骤参数混入该配方，未知 confirmed 保持 null。source 包含助手消息 UUID 和执行快照摘要。固定资产的 dependency digest 来自已验证内容；模型/工具 digest 覆盖冻结配置声明，没有上游权重/实现 revision 的依赖仍标记 unversioned，不承诺精确复现。模板变量、必填值、示例和事实绑定都校验。

### 7.1 显式 attempt 重试

```json
{
  "recipe_request_id":"recipe-001",
  "output_ids":["final_text"],
  "context":"整理本次能力说明",
  "retry_command_id":"recipe-retry-001",
  "expected_attempt":1
}
```

- 普通查询失败记录不隐式重试。只有 failed/interrupted 可开启 attempt；原输入摘要不变。
- retry_command_id 与 expected_attempt 一起提供；同 retry 命令重投返回它关联的 attempt，不重复调用 LLM。
- 并发不同命令争用同 expected_attempt 只有一个成功，其余 RECIPE_ATTEMPT_CONFLICT。
- attempt 历史、失败原因和 retry 回执先持久提交，再调用 LLM；每次使用第一次冻结的事实。
- 成功后基础键持续返回成功结果，不允许重试覆盖成功。content_revision 与 attempt/backend 编辑 version 分开。
- 构建取得 Recipe 槽后默认超时 120 秒，包含共享 LLM 限流、连接等待及重试；不含 Recipe 槽排队。

后端草稿/编辑 UUID 和版本自管。只有取得结构化结果并 DB 提交后返回前端 201；HTTP 等待超预算时保留原键并明确暂态错误，下次继续查构建，不能返回空 DRAFT 或盲目再 retry。

### 7.2 后端 Skill 包规范

后端在固定编辑版本上确定性构建 manifest、SKILL.md 和 recipe.json，记录每个文件 SHA-256；只允许安全相对路径，拒绝绝对路径、`..`、符号链接和可执行脚本。完成权限/变量/引用校验后调用既有 SkillService.CreateSkill/UpdateSkill；本服务不上传文件、不替代发布/审核；资产发布职责不并入配方构建接口。

复用已编辑 Recipe 时，backend 将变量与参数渲染为 effective，并提供受控声明式资产快照。当前 Recipe 资产快照格式为 schema_version=1、prompt_template、variables_schema、parameters、可选 negative_prompt_template/dependencies；backend 将参数渲染到 effective 后，资产快照的 parameters 必须为空对象，非空会拒绝，而不是被服务暗中覆盖或忽略。Skill content 只接受 instructions 和 variables_schema；变量 Schema 必须为 object，不允许远程引用/$id/$dynamicRef。不从外部 URL 解析资产或凭据。

## 8. 统一配置、依赖与 EAS

唯一配置：[service.yaml](aigc/configs/service.yaml)。Settings.load() 校验嵌套配置，ServiceCredentials 单独加载，不合并其他默认 YAML。不支持旧配置文件、别名、API_TOOLS_CONFIG、AIGC_QUEUE_LIMIT。

| 配置段 | 当前作用 |
|---|---|
| server/auth | 单 worker、端口/HTTP 并发、SSE 回源 keep-alive、应用鉴权 |
| runtime/events | 活跃/在途配额、每用户限制、交互期限、SSE 512 上限、128 条批次及连接时长 |
| capabilities | model_ref 映射、modality/操作、允许参数/size 组合、confirmed/poster 响应路径、上下文/资产预算 |
| settlement | 查询监督器并发 8、总预算 600 秒、单次查询 60 秒、5 秒间隔；不支持远端取消 |
| upstreams | LLM 32、图片提交 8、轮询 32、收尾 HTTP 4（8 条连接）的独立并发/RPM/连接池及重试 |
| recipe | 并发 1、在途 100、构建超时 120 秒 |
| storage | journal、恢复索引、缓存 256 条/64MiB、8 个 IO 线程、可靠 flock |
| agent | 规划提示词、模型、回合/token 上限；没有动态 session memory/历史入口 |
| observability/eas/secrets | 指标、探针/部署、凭据引用 |

默认 Run 活跃 64、总在途 500、每用户活跃 4/在途 20。计算完成但未核清的 Run 仍计入服务在途配额，不能让未知副作用无限突破准入。RPM=0 表示没有本地 RPM 限制，不表示远端无限额；多个 Pod 共用凭据仍需 backend 账户 RPM/TPM 预算。

### 8.1 部署与扩容

1. [Dockerfile](../docker/aigc.Dockerfile) 构建 `linux/amd64`（目标 x86 机型），推送同地域 ACR 内网仓库；应用端口 8000，避开 EAS 8080/9090。
2. 填写 eas.image/nas_server/nas_path/gateway，生产专属网关需 gateway_id。预建 NAS shard-N、config、secrets；配置和密钥只读挂载，数据给 UID/GID 10001 写权限。
3. 生成 JSON，不创建云资源：

   ```bash
   AIGC_CONFIG=/absolute/path/service.yaml python -m service.aigc.deploy eas --shard 0 --output eas-service.json
   ```

4. 使用真实 EAS 控制台/eascmd 部署，配置 VPC/ACR/出网权限、NAT 与上游账户配额。生成器不替代这些平台配置。
5. 每服务固定一个 Pod；NAS 要求 NFSv4 跨客户端 flock，拒绝本地锁退化/FUSE。epoch 检测不构成分布式 fencing。生成器不设置 NFS 协议选项，实际挂载需验证。
6. 配置 startup/readiness/liveness、SIGTERM、先停后起滚动策略。ALB 不支持用 rpc.keepalive 修改实际请求超时，需人工对齐；NLB 生成当前拒绝。
7. 人工横向扩容按稳定 user→shard 路由增加独立服务/目录，新用户去低负载分片；不要 hash(user)%N 自动重映射在途用户。迁移和数据清理必须单独授权。

### 8.2 运维与验收

- `/health/live`：全局台账故障返回 503；`/health/ready`：受理中、健康、持租约，返回 config_digest。
- `/internal/stats` 使用同一服务鉴权和 X-User-Id，返回 Pod 级统计，不向终端用户开放。
- 不自动清理 journal、取消墓碑或历史 attempt。当前新台账 payload 标识为 schema_version=1；缺少此标识的早期格式及此前误标 2 的联调记录均不自动转换，访问返回 PROTOCOL_VERSION_UNSUPPORTED，保留原数据、不重新执行。路径恢复不等于旧请求/旧记录兼容；处理已有联调数据须另行明确授权。
- 事件缓存只保存已提交事实；冷记录从 journal 重放。常规热事件有 journal/目录同步，快照/受理/终态和索引还需额外 IO，不宣称全部提交恒 O(1)。
- [本地压测](../.dev_scripts/aigc/loadtest.py) 只使用 mock 上游，参数按用户/会话/Run、活跃/在途容量配置；不代表 EAS 生产吞吐。
- [EAS 探针](../.dev_scripts/aigc/eas_probe.py) 在隔离服务验证流、鉴权、锁与挂载；真实 EAS、BYOK、视频、永久 URL 和 backend Go 联调尚需环境/实际契约。
- [Compose](../docker/aigc.compose.yaml) 与 Docker HEALTHCHECK 默认 8000；修改端口须同步健康命令，不能用 Docker HEALTHCHECK 代替 EAS 探针。

### 8.3 本地验证与联调门槛

```bash
python -m pytest -o asyncio_default_fixture_loop_scope=function tests/service/aigc tests/session/test_run_store.py tests/tools/test_api_tools.py
python .dev_scripts/aigc/loadtest.py --users 8 --sessions 2 --runs 3 --mode burst --image-ratio 0.5 --recipe-ratio 0.5 --sse-delay 0.01 --output /tmp/aigc-agentic-load.json
docker build --platform linux/amd64 -f docker/aigc.Dockerfile -t ms-agent-aigc:dev .
python .dev_scripts/aigc/container_smoke.py
```

压测仅启动本地 mock LLM/图片 API，使用当前 `/v1/aigc` 的 UUID/冻结输入、精确授权和 run.completed 水位；支持 SSE/轮询、慢事件读取、429 注入和 Recipe 共享 LLM 竞争。报告包含成功/失败、GetRun/SSE p95、RSS、事件循环延迟、fsync/journal 读取次数。容器冒烟只创建一次性本地卷与 mock 凭据，验证只读根目录、非 root、崩溃恢复/幂等、租约互斥及旧路径拒绝；不会操作已有执行数据。

2026-11-06 版本恢复验证：定向测试 119 项通过，包含业务接口路径、OpenAPI/公开 schema 固定值、台账与事件版本一致性、README 示例/链接校验及不匹配联调记录安全拒绝；相关 SDK 回归 241 项通过、66 项跳过。当前 `/v1/aigc` 本地 mock 冒烟的 8 个 Run、5 个 Recipe 全部成功（`temp/aigc-agentic-v1-check.json`）。Docker daemon 未运行，本轮未完成镜像重建或容器复验；不能沿用旧镜像声称已验证恢复后的入口。

上一轮能力改造的定向契约测试 115 项通过；SDK 离线回归 424 项通过、86 项跳过，另排除依赖真实模型凭据的两个旧 memory 测试文件。以下性能表为 2026-11-05 的历史联调样本，接口标签曾误写为 v2，不能作为本次版本恢复后的新验收结果。上一轮 `ruff check .`、变更 Python 文件的格式校验及 `git diff --check` 通过；全仓 `make lint` 的格式阶段仍受既存 Markdown 格式问题影响，未扩大格式化范围。

同机顺序执行的固定 mock 样本（8 用户 × 2 会话 × 3 Run，28 次图片提交、76 次 LLM 调用，无 429/Recipe）：

| 指标 | 原实现基线 ba6b49f2 | Agentic 改造后历史样本 |
|---|---|---|
| 成功 Run | 48/48 | 48/48 |
| Run/分钟 | 167.68 | 167.04 |
| 峰值 RSS MiB | 79 | 81 |
| 最终 GetRun p95 ms | 2.7 | 2.2 |
| loop lag p99 ms | 15 | 12 |
| 事件数 / fsync 每事件 | 588 / 3.73 | 844 / 2.73 |

这是单次本地样本，不代表统计改善或生产吞吐上限；默认工具轮询间隔主导 Run 时长，Agentic 改造后的事件语义更丰富。新版额外度量全部轮询 GetRun p95（本样本 50.5 ms），不能与旧脚本只度量最终 GET 的字段混比。独立慢 SSE + Recipe 样本的 48 个 Run、28 个 Recipe 全部成功；429 注入样本也完成 48 个 Run。amd64 镜像及非 root/只读容器崩溃恢复冒烟通过。结果保存在本地 `temp/aigc-v1-fixed.json`、`temp/aigc-v2-fixed.json`、`temp/aigc-v2-load-sse.json`、`temp/aigc-v2-load-poll.json`，不作为发布产物或线上保证。

发布前仍需外部环境与授权验证：真实 EAS/NAS 跨客户端锁及 fsync、网关 SSE/鉴权与 SIGTERM、BYOK 归属/凭据解析、视频实际响应、永久 URL 和后端 DB 活动位/事件投影。不得用本地 mock 通过代替生产验收。

## 9. 错误与后端同步

| HTTP | code / 场景 |
|---|---|
| 400 | EVENT_CURSOR_INVALID，游标不合法；USER_ID_INVALID，可信用户标识不合法 |
| 400/422 | INPUT_INVALID，非法字段/UUID/请求结构 |
| 401 | UNAUTHORIZED；网关错误不保证应用 JSON 包装 |
| 404 | RUN_NOT_FOUND/OUTPUT_NOT_FOUND，不存在或不属于用户 |
| 409 | RUN_INPUT_CONFLICT、COMMAND_KEY_CONFLICT、RECIPE_INPUT_CONFLICT/RETRY_CONFLICT/ATTEMPT_CONFLICT |
| 409 | INTERACTION_EXPIRED、RUN_STATE_CONFLICT、SOURCE_RUN_NOT_RETRYABLE、SOURCE_RUN_NOT_TERMINAL、RETRY_SNAPSHOT_CONFLICT |
| 409 | PROTOCOL_VERSION_UNSUPPORTED，旧记录不可变为新版执行 |
| 422 | CAPABILITY_NOT_SUPPORTED、CONTEXT_TOO_LARGE、INVALID_INTERACTION_INPUT |
| 413 | REQUEST_TOO_LARGE，默认 1MiB；网关可能更早拒绝 |
| 429 | RUN_QUEUE_FULL（scope=global/user）、EVENT_SUBSCRIBERS_FULL、RECIPE_QUEUE_FULL；遵守 Retry-After |
| 503 | DEPENDENCY_UNAVAILABLE（未配置 BYOK/缺失资产）、SERVICE_UNAVAILABLE、STORAGE_UNAVAILABLE、STORAGE_CORRUPTED；不能仅凭 HTTP 推断原任务未执行 |

Run/Recipe 的 error 在查询成功的 200 中返回，常见 EXECUTION_FAILED、INTERACTION_TIMEOUT、DEADLINE_EXCEEDED、EXTERNAL_TASK_UNRESOLVED、SERVICE_INTERRUPTED、RECIPE_BUILD_FAILED。settlement.error 可为 SETTLEMENT_UNRESOLVED。

后端对接顺序：DB 持久受理→后台 StartRun→GetRun 恢复关联→SSE 有序应用或完整快照→精确 ControlRun→核清 can_release_activity 后保存最终消息并释放活动位。超时、断流、202 和 APPLIED 不代替这一流程。GetOrBuildRecipe 复用基础键，显式重试才开启 attempt；Skill 编辑/打包/发布保持 backend 职责。

### 9.1 接口联调响应节选

以下为响应字段节选，完整严格对象以 OpenAPI 类型为准：

```json
{"schema_version":1,"status":"queued","state_revision":1,"last_agent_seq":1,"snapshot_agent_seq":1,"execution_closed":false,"can_release_activity":false}
```

StartRun 首次返回 202；GetRun 返回相同结构的完整 RunView（200/304）。后端收到快照后保存其关联、public_snapshot 与 snapshot_agent_seq，再订阅该水位之后的 SSE；SubscribeRunEvents 帧见第 5 节。快照和事件只能使用同一个 Run 有序 reducer，不能混用另一份 session 历史。

```json
{"command_id":"stop-001","action":"stop","command_status":"applied","reason_code":"APPLIED","run_status":"stopping","execution_closed":false,"settlement_status":"tracking","can_release_activity":false,"remote_cancel_supported":false,"remote_task_may_continue":true}
```

这是在途任务的 ControlRun 200 回执示意；远端结果仍未核清，必须继续 GetRun/SSE。只有最终 can_release_activity=true 才能释放后端活动位。

```json
{"schema_version":1,"recipe_request_id":"recipe-001","attempt":2,"status":"queued","result":null,"error":null,"retry_receipts":[{"retry_command_id":"recipe-retry-001","expected_attempt":1,"attempt":2}]}
```

这是 GetOrBuildRecipe 显式 retry 的 202 响应示意；同 retry ID 重投仍返回 attempt 2，不再调用 LLM。另一个 retry ID 竞争 expected_attempt=1 返回 409/RECIPE_ATTEMPT_CONFLICT；后端继续查原基础键，不创建第二草稿。成功响应的 result 类型见第 7 节，只有 backend DB 提交后才对前端返回 201。

## 10. 版本与变更记录

- 当前接口前缀固定 `/v1/aigc`，服务/OpenAPI 声明固定 `1.0.0`；输入、事件、公开快照和 Recipe 的 schema_version 均为 1，统一源为 SDK `protocol.py`。
- 目前仅联调，未上线；增加功能或刷新文档不自动升版。任何接口版本变更须先向用户说明影响范围并获得明确审批，不能由助手自行决定。
- 文档按修订日期及变更记录跟踪，不等同于接口发布。content_revision、attempt、state_revision、backend version 与 agent_seq 各自独立，不能混作接口主版本。
- 正式交付记录 Git SHA、镜像 digest、config_digest 与真实验收范围，不把 mock 或开发提交当作线上保证。

| 日期 | 修订 / 基线 | 变更 |
|---|---|---|
| 2026-11-06 | 联调版本恢复与文档刷新 | 按用户要求恢复 /v1、服务 1.0.0 与 schema 1；同步实现/脚本/测试；保留已实现功能；接口升版需事先审批；纠正参数范围、错误码、追踪头和退出预算说明 |
| 2026-11-05 | Agentic 边界补齐（未发布） | SSE 独立回放/去重与批次、同水位回执、HTTP 调用槽前门闩、独立收尾限流、原停止原因恢复、所选产物 Recipe 事实与重试竞争；压测/容器脚本及中英 README 迁移 |
| 2026-11-05 | Agentic 工作区改造（未发布） | 完整冻结输入、显式分支、Custom 固定执行、同水位公共 reducer、action_id、submission intent、有限原任务收尾、Recipe attempt/CAS；曾误标 v2/2.0.0，已按 2026-11-06 要求恢复原接口版本；外部未配置能力明确拒绝 |
| 2026-11-05 | 文档 1.1.0，核对 ba6b49f2 | 澄清 v1 自动历史/memory、受理快照、SSE 与停止边界，增加版本管理；本行是历史文档修订，不表示当前请求结构退回早期格式 |
| 2026-11-05 | ba6b49f2 | 删除旧工具配置/Settings 别名/optimized 双轨与 LLM/urllib 回退 |
| 2026-11-05 | 327086e9 | 单 Pod 公平调度、SSE 唤醒/ETag、缓存/索引、上游限流、EAS 生成器 |
| 2026-11-05 | 605f3e74 / 2ff24172 | 初版接口说明书及持久幂等、取消墓碑、文件台账基础实现 |

本轮不修改后台 Go/V3 原始文档、不创建 tag、不 commit/push 或部署云资源。联调 backend 需对齐当前 `/v1/aigc` 的最新类型，增补 interrupted/settlement/活动位判据，并将 Skill 打包与长上下文处理按本协议调整。
