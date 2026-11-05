# AIGC 服务接口说明

## 1. 概述

AIGC 服务将 MS-Agent 的轻量执行能力封装为 HTTP 接口，供可信业务 backend 调用。接口层位于 `service/aigc`，运行、工具调用、会话、记忆及配方构建由 `ms_agent` 核心模块实现，部署方式为独立 Docker 镜像。

```text
业务 backend → Docker 服务 → service/aigc 接口层 → ms_agent 轻量核心 → 已配置的模型/工具 API
```

| 项目 | 说明 |
|---|---|
| 服务版本 | `1.0.0` |
| 业务接口前缀 | `/v1/aigc` |
| 本地访问地址 | `http://127.0.0.1:8000`；实际地址由部署环境决定 |
| 请求格式 | `application/json`，UTF-8 |
| 普通响应格式 | `application/json` |
| 事件响应格式 | `text/event-stream`，SSE |
| 在线接口文档 | `/docs` |
| OpenAPI 描述 | `/openapi.json` |
| 执行 profile | 当前仅接受 `aigc_assistant` |

本文按当前实现描述接口。示例中的用户、UUID、摘要、时间、任务 ID 和图片地址均为示意，不代表真实执行结果。

### 1.1 接口目录

| 接口名 / operationId | 方法 | 完整路径 | 功能 |
|---|---|---|---|
| StartRun | POST | `/v1/aigc/runs` | 持久幂等地受理一次执行 |
| GetRun | GET | `/v1/aigc/runs/{run_uuid}` | 查询状态、关联、回执和已提交结果 |
| SubscribeRunEvents | GET | `/v1/aigc/runs/{run_uuid}/events` | 订阅执行事件及按序号回放 |
| ControlRun | POST | `/v1/aigc/runs/{run_uuid}/commands` | 授权、跳过、补充输入、干预或停止 |
| GetOrBuildRecipe | POST | `/v1/aigc/runs/{run_uuid}/recipe` | 获取或异步生成可编辑配方 |

## 2. 公共约定

### 2.1 认证与请求头

所有业务接口均需要以下请求头：

| 请求头 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `Authorization` | string | 是 | `Bearer <service-token>`，用于服务间认证 |
| `X-User-Id` | string | 是 | 可信 backend 解析登录身份后注入的用户标识 |
| `Content-Type` | string | POST 时是 | `application/json` |
| `Accept` | string | 否 | 普通请求使用 `application/json`；事件订阅建议使用 `text/event-stream` |
| `Last-Event-ID` | string | 否 | 仅用于事件订阅，表示客户端已处理的最后一个事件序号 |

安全边界：

- `service-token` 是服务间凭据，不是用户登录令牌，也不是模型或图片 API Token。
- 服务信任持有服务凭据的 backend 所传递的 `X-User-Id`；不自行校验终端用户登录会话。
- 不向浏览器或不可信客户端暴露服务凭据；业务 backend 必须自行校验用户身份与业务权限。
- 请求体不能传入 `user_id`。服务根据 `X-User-Id` 访问对应用户的数据；查询其他用户的 Run 返回 `404 RUN_NOT_FOUND`。
- 所有用户、Run、事件、命令及 Recipe 请求应持续使用同一用户上下文。

响应头 `X-Request-Id` 是服务为本次 HTTP 请求生成的追踪标识。它不参与幂等判断，不等同于 `run_uuid`、`command_id` 或外部 API 的 `request_id`。

### 2.2 标识、时间与字段校验

| 标识 | 约束 / 用途 |
|---|---|
| `run_uuid` | 调用方生成的合法 UUID 字符串；服务规范化后使用 |
| `session_id` | 调用方指定的会话标识；同一用户下用于关联历史并串行执行 |
| `command_id` | 调用方生成的控制命令标识；作用域为当前用户、当前 Run |
| `recipe_request_id` | 调用方生成的配方构建请求标识；作用域为当前用户，不局限于单个 Run |
| `interaction_id` | 服务生成的交互标识，须取自当前 `pending_interaction` |
| `artifact_id` | 服务返回的产物标识；作为不透明字符串使用，不自行拼接 |
| `recipe_uuid` | 服务生成的配方标识；不替代调用方的幂等键 `recipe_request_id` |

`session_id`、`command_id`、`recipe_request_id` 长度为 1～128，格式为 `^[A-Za-z0-9_:@-][A-Za-z0-9_.:@-]{0,127}$`。

`X-User-Id` 长度为 1～128，只允许 ASCII 字母、数字及 `_ . : @ -`，不能为 `.` 或 `..`。所有标识均不能包含路径分隔符。

时间使用 ISO 8601 格式，例如 `2026-11-05T08:00:00+00:00`。摘要由服务生成，当前采用 SHA-256；调用方应存储返回值，不自行推断其内容。

对外结构化请求拒绝未声明的字段，不将字符串自动转换为数字或布尔值。例如 `max_rounds` 应传 JSON 整数，不传 `"20"`。`payload` 等开放对象的具体内容另按交互规则校验。

### 2.3 异步与持久化语义

- `202 Accepted` 只表示请求已受理或命令已接收，不代表执行完成或动作已生效。
- `200 OK` 表示 HTTP 请求成功，也可能返回仍在运行的已有 Run，或失败/中断的 Recipe 构建记录。必须继续检查响应体的 `status`。
- Run 按 `(X-User-Id, run_uuid)` 幂等；命令按 `(X-User-Id, run_uuid, command_id)` 幂等；配方构建按 `(X-User-Id, recipe_request_id)` 幂等。
- 同一幂等键、相同语义输入返回原记录；同一幂等键、不同输入返回 `409`。请求超时或回执丢失时，应先查询或使用原幂等键重试。
- 相同用户和会话的 Run 按受理顺序串行执行；不同会话可受控并发。用户间采用轮询公平调度，并设每用户活跃/在途上限。会话历史和 memory 按用户/会话隔离。
- 等待授权或补充输入时归还活跃执行槽，仍计入在途容量；默认 1800 秒超时后进入 `failed / INTERACTION_TIMEOUT`。配置为 0 可关闭超时。
- 默认上下文只保留最近 40 条消息、最多 60,000 字符的完整用户回合，不拆开 tool call 与结果；会话文件仍保存完整历史。memory 只召回窗口之外的内容，默认不生成 MEMORY.md 投影。
- 本地文件与 Docker 持久卷保存执行台账、事件、回执和结果。持久卷丢失后无法保持原幂等和回放保证。
- 服务重启后，未完成的 Run/Recipe 标记为 `interrupted`，不自动续跑或重新提交图片任务。确需新的执行时使用新的 `run_uuid`，确需新的配方生成时使用新的 `recipe_request_id`。
- 外部图片 API 没有已确认的幂等键或取消接口，本服务不承诺外部副作用 exactly-once，也不承诺停止后远端已取消生成或扣费。

### 2.4 默认限制

| 项目 | 默认值 / 限制 |
|---|---|
| JSON 请求体大小 | 最大 1 MiB；超出返回 413；EAS 网关可能先行拒绝 |
| Run 活跃并发 | 64，可配置；等待交互不占活跃槽 |
| Run 在途容量 | 500，含排队、执行和等待交互 |
| 每用户活跃 / 在途容量 | 4 / 20，同一用户不同 session 共用配额 |
| LLM 并发 | 32，Run 与 Recipe 共用；RPM 必须按实际账户配额配置 |
| 图片提交 / 轮询并发 | 8 / 32，分别限流 |
| SSE 订阅上限 | 512，单流默认最长 540 秒，心跳 15 秒 |
| 单次 messages 数量 | 1～20 |
| 单条 content 长度 | 1～20,000 字符 |
| 单条消息图片附件数 | 最多 4 |
| Recipe 并发 | 1，可配置 |
| Recipe LLM 调用超时 | 120 秒，可配置；不含排队等待时间 |
| Recipe 在途构建数量 | 当前实现上限 100，含排队/执行中的构建 |

## 3. Run 状态

| status | 含义 | 终态 |
|---|---|---|
| `missing` | 尚未绑定 StartRun 输入；可能已有被拒绝的提前控制命令记录 | 否 |
| `queued` | 已受理，等待调度或同一会话的前序 Run | 否 |
| `running` | 正在执行 | 否 |
| `waiting_input` | 等待授权、跳过或补充参数，详见 `pending_interaction` | 否 |
| `stopping` | 停止命令已接收，本地执行正在退出 | 否 |
| `succeeded` | 执行成功完成 | 是 |
| `failed` | 执行失败，或外部任务结果未确认而停止继续规划 | 是 |
| `cancelled` | 本地执行已取消；不等于远端任务已取消 | 是 |
| `interrupted` | 服务中断或本地状态不可用，未自动续跑 | 是 |

常规链路：`queued → running ↔ waiting_input → succeeded / failed`。

停止链路：`running / waiting_input → stopping → cancelled`。启动前停止可直接生成 `cancelled` 墓碑。

终态不能重新进入执行状态。重复 StartRun 返回原终态，不能用它重置或恢复已结束的执行。

## 4. 公共响应对象

### 4.1 RunView

StartRun 和 GetRun 均返回 RunView，无额外的 `data/success` 包装。

| 字段 | 类型 | 说明 |
|---|---|---|
| `run_uuid` | string | 执行标识 |
| `session_id` | string / null | 关联会话；启动前取消墓碑尚未绑定会话时可为空 |
| `profile_id` | string / null | 执行 profile；当前为 `aigc_assistant` |
| `status` | string | Run 状态，见第 3 节 |
| `request_digest` | string / null | 规范化请求的摘要；尚未 StartRun 时可为空 |
| `snapshot_digest` | string / null | 首次输入与服务配置、工具、skills 快照的摘要 |
| `last_agent_seq` | integer | 已提交的最新公开事件序号；不是会话消息 seq |
| `pending_interaction` | object / null | 当前需要处理的交互 |
| `result` | object / null | SDK 执行结果；中断时也可能保留已提交的部分结果 |
| `artifacts` | array<object> | 已提交产物；无产物时为 `[]` |
| `error` | object / null | Run 的业务错误，包含 `code`、`message` |
| `external_tasks` | array<object> | 已提交外部任务或提交结果未确认的记录 |
| `commands` | object | 以 `command_id` 为键的最新命令回执映射 |
| `requires_reconciliation` | boolean | 需调用方核对执行/外部任务；不等于已经执行了自动对账 |
| `created_at` | string | 创建时间 |
| `updated_at` | string | 最新提交时间 |
| `links` | object | `self`、`events`、`commands`、`recipe` 四个相对资源路径 |

### 4.2 result：SDK 执行结果

| 字段 | 类型 | 说明 |
|---|---|---|
| `session_id` | string | 结果关联会话 |
| `messages` | array<object> | 消息集合，每项包含 `role`、`content`、`tool_calls` |
| `messages[].role` | string | SDK 返回的消息角色；不据此单独判定 Run 成功 |
| `messages[].content` | string | 消息正文 |
| `messages[].tool_calls` | array<object> / null | 调用描述：`id`、`name`、`arguments` |
| `tool_calls_made` | array<object> | 工具调用记录，每项包含 `tool_name`、`arguments`、`result`、`duration_ms` |
| `usage` | object | `prompt_tokens`、`completion_tokens`、`total_tokens`、`rounds` 整数统计 |
| `metadata` | object | 扩展信息；`partial: true` 表示已提交的部分结果，不能当成成功终态 |

工具记录中的 `result` 包含：

| 字段 | 类型 | 说明 |
|---|---|---|
| `tool_name` | string | 工具名 |
| `success` | boolean | 本次工具调用是否成功，不等同于整个 Run 的 status |
| `content` | string | 工具结果文本 |
| `raw_data` | object / null | 原始结构化 API 结果；字段由实际工具契约决定 |
| `error` | string / null | 工具错误描述 |
| `metadata` | object | 可包含 `task_id`、`duration_ms`、`unresolved`、`delivery_unknown` 等信息 |

HTTP 成功响应、Run 成功、工具成功、外部任务成功是不同概念。判断执行是否结束以 Run 的 `status` 为准，不能只看有无 `result`。

### 4.3 Artifact：产物

| 字段 | 类型 | 适用类型 | 说明 |
|---|---|---|---|
| `artifact_id` | string | 全部 | 不透明的产物标识 |
| `type` | string | 全部 | 当前为 `text` 或 `image` |
| `media_type` | string | 全部 | 文本为 `text/plain`，图片当前为 `image/*` |
| `content` | string | text | 已提交文本内容；当前最终文本产物 ID 为 `final_text` |
| `url` | string | image | 外部 API 返回的图片地址 |
| `invocation_id` | string | image | 来源工具调用标识 |
| `tool_name` | string | image | 来源工具名，例如 `image_generation` |

服务不自动下载或代理图片。图片 URL 的有效期、访问权限和长期可用性由远端服务决定，响应不保证有文件大小、校验和或有效期字段。

### 4.4 pending_interaction：待处理交互

| 字段 | 类型 | 说明 |
|---|---|---|
| `interaction_id` | string | 当前交互 ID |
| `kind` | string | `approval`：授权；`input`：补充/修正工具参数 |
| `invocation_id` | string | 对应工具调用 |
| `tool_name` | string | 对应工具名 |
| `arguments` | object | 当前工具参数 |
| `input_schema` | object | 仅 `kind=input` 时提供，采用 JSON Schema 描述完整有效参数 |

同一 Run 的交互按执行顺序出现。补充输入生效后，可能产生新的授权交互；必须重新读取新的 `interaction_id`，不能沿用上一交互 ID。

### 4.5 external_tasks：外部任务记录

记录可能包含 `invocation_id`、`tool_name`、`task_id`、`request_id`、`task_status`、`remote_cancel_supported`、`requires_reconciliation`。

提交成功且收到 `task_id` 时，该 ID 按字符串保存；提交网络超时等情况下可能只记录提交未确认，不能假定每条记录都有 `task_id`。`request_id` 是外部 API 的追踪标识，不是执行 ID。

本服务仅暴露已记录状态，不通过 GetRun 自动查询远端任务。`requires_reconciliation=true` 时，调用方应根据已有 `task_id` 等信息核对；不要为等待结果而重新发起生成。

## 5. StartRun：受理执行

### 5.1 基本信息

| 项目 | 值 |
|---|---|
| 方法 | POST |
| 路径 | `/v1/aigc/runs` |
| operationId | `StartRun` |
| 认证 | Bearer + X-User-Id |
| 请求体 | StartRunRequest |
| 响应体 | RunView |

### 5.2 请求参数

| 字段 | 类型 | 必填 | 默认值 / 约束 |
|---|---|---|---|
| `run_uuid` | string | 是 | 合法 UUID；调用方为同一次业务执行固定生成 |
| `session_id` | string | 是 | 安全会话标识，见 2.2 |
| `profile_id` | string | 否 | `aigc_assistant`，当前仅允许此值 |
| `messages` | array<object> | 是 | 1～20 条消息 |
| `parameters` | object | 否 | 默认 `{}`；仅允许下表字段 |

消息字段：

| 字段 | 类型 | 必填 | 默认值 / 约束 |
|---|---|---|---|
| `role` | string | 否 | 默认 `user`，当前仅允许 `user` |
| `content` | string | 是 | 长度 1～20,000 |
| `attachments` | array<object> | 否 | 默认 `[]`，最多 4 张图片 |
| `attachments[].type` | string | 否 | 默认 `image`，当前仅允许 `image` |
| `attachments[].url` | string | 是 | 长度 1～10,000，以 `http://` 或 `https://` 开头，须可由图片 API 读取 |

不接受本地图片路径、独立 Base64 字段或 Data URI 附件。参考图顺序保留，但服务不保证任何模型都能执行图片编辑，应由部署方配置具备权限和对应能力的模型。

parameters 字段：

| 字段 | 类型 | 必填 | 约束 / 行为 |
|---|---|---|---|
| `max_rounds` | integer / null | 否 | 1～100；不传或 null 时使用服务端配置，默认配置为 20 |
| `system_prompt` | string / null | 否 | 最多 20,000 字符；不传或 null 时使用服务端提示词 |
| `temperature` | number / null | 否 | 0～2；不传或 null 时不覆盖服务端模型配置 |

不能通过本接口设置模型 API 地址、凭据、工具注册、非核心模块或任意代码。图片模型和调用参数由后续工具调用与服务端工具 schema 决定。

### 5.3 请求示例

```bash
curl -X POST "${AIGC_BASE_URL}/v1/aigc/runs" \
  -H "Authorization: Bearer ${AIGC_SERVICE_TOKEN}" \
  -H "X-User-Id: user-001" \
  -H "Content-Type: application/json" \
  -d '{
    "run_uuid": "11111111-1111-4111-8111-111111111111",
    "session_id": "session-001",
    "profile_id": "aigc_assistant",
    "messages": [{"role": "user", "content": "介绍 AIGC 助手支持的能力"}],
    "parameters": {"max_rounds": 20, "temperature": 0.7}
  }'
```

其中 `AIGC_BASE_URL` 为服务根地址，例如 `http://127.0.0.1:8000`，不包含 `/v1/aigc`。

图片编辑消息示例：

```json
{
  "role": "user",
  "content": "给参考图中的猫戴上生日帽",
  "attachments": [{"type": "image", "url": "https://example.com/reference-cat.png"}]
}
```

### 5.4 响应与幂等

| HTTP 状态 | 含义 |
|---|---|
| 202 | 新执行已持久受理，状态为 queued |
| 200 | 返回已有 Run，或返回启动前已取消的 Run |
| 409 | 同一用户下 run_uuid 已绑定不同语义输入 |
| 422 | UUID、请求字段、类型或参数范围无效 |
| 429 | 运行任务容量已满 |
| 503 | 服务暂不受理或本地存储不可用 |

首次受理响应示例：

```json
{
  "run_uuid": "11111111-1111-4111-8111-111111111111",
  "session_id": "session-001",
  "profile_id": "aigc_assistant",
  "status": "queued",
  "request_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "snapshot_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "last_agent_seq": 1,
  "pending_interaction": null,
  "result": null,
  "artifacts": [],
  "error": null,
  "external_tasks": [],
  "commands": {},
  "requires_reconciliation": false,
  "created_at": "2026-11-05T08:00:00+00:00",
  "updated_at": "2026-11-05T08:00:00+00:00",
  "links": {
    "self": "/v1/aigc/runs/11111111-1111-4111-8111-111111111111",
    "events": "/v1/aigc/runs/11111111-1111-4111-8111-111111111111/events",
    "commands": "/v1/aigc/runs/11111111-1111-4111-8111-111111111111/commands",
    "recipe": "/v1/aigc/runs/11111111-1111-4111-8111-111111111111/recipe"
  }
}
```

摘要基于规范化输入计算：默认字段补齐，parameters 内的 null 项不作为覆盖值保存。首次冻结服务配置/工具/skills 快照；后续同 ID 重试不会覆盖快照或读取新配置重新执行。更改消息、会话、有效参数等必须使用新的 run_uuid，否则返回冲突。

## 6. GetRun：查询执行

### 6.1 基本信息与请求

| 项目 | 值 |
|---|---|
| 方法 | GET |
| 路径 | `/v1/aigc/runs/{run_uuid}` |
| operationId | `GetRun` |
| 路径参数 | `run_uuid`：必填，UUID |
| 请求体 / 查询参数 | 无 |
| 响应体 | RunView |

```bash
curl "${AIGC_BASE_URL}/v1/aigc/runs/11111111-1111-4111-8111-111111111111" \
  -H "Authorization: Bearer ${AIGC_SERVICE_TOKEN}" \
  -H "X-User-Id: user-001"
```

### 6.2 响应

成功返回 200，完整结构见 4.1。终态响应节选：

```json
{
  "run_uuid": "11111111-1111-4111-8111-111111111111",
  "session_id": "session-001",
  "status": "succeeded",
  "last_agent_seq": 7,
  "pending_interaction": null,
  "artifacts": [{
    "artifact_id": "final_text",
    "type": "text",
    "media_type": "text/plain",
    "content": "支持轻量对话以及已配置的图片生成与编辑能力。"
  }],
  "commands": {},
  "error": null,
  "requires_reconciliation": false
}
```

Run 不存在或不属于当前用户时返回 404：

```json
{
  "code": "RUN_NOT_FOUND",
  "message": "RUN_NOT_FOUND",
  "request_id": "example-http-request-id",
  "details": null
}
```

GetRun 不调用模型、不启动执行、不自动重提或查询外部任务。适用于启动回执丢失、服务重启、轮询状态及命令回执核对。

响应携带 `ETag`（完整公开视图的摘要）。同用户查询可传 `If-None-Match`；视图未变化时返回无正文的 `304 Not Modified`。调用方按用户/Run 隔离缓存，不能跨用户复用；命令回执变化也会使 ETag 变化。

## 7. SubscribeRunEvents：订阅与回放事件

### 7.1 请求参数

| 项目 | 值 |
|---|---|
| 方法 | GET |
| 路径 | `/v1/aigc/runs/{run_uuid}/events` |
| operationId | `SubscribeRunEvents` |
| 响应类型 | `text/event-stream` |

| 参数 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `run_uuid` | path | UUID string | 是 | 执行标识 |
| `after_seq` | query | integer | 否 | 排他游标，只返回 `agent_seq > after_seq` 的事件 |
| `Last-Event-ID` | header | integer string | 否 | 未提供 after_seq 时使用 |

优先级：`after_seq → Last-Event-ID → 0`。同时提供时，以 after_seq 为准。

游标须位于 `0～last_agent_seq`。`Last-Event-ID` 非整数、负数或游标超过末尾返回 `400 EVENT_CURSOR_INVALID`；查询参数类型/非负校验失败返回 `422 INPUT_INVALID`。

### 7.2 请求和响应示例

```bash
curl -N "${AIGC_BASE_URL}/v1/aigc/runs/11111111-1111-4111-8111-111111111111/events?after_seq=2" \
  -H "Authorization: Bearer ${AIGC_SERVICE_TOKEN}" \
  -H "X-User-Id: user-001" \
  -H "Accept: text/event-stream"
```

```text
id: 3
event: step_started
data: {"schema_version":1,"run_uuid":"11111111-1111-4111-8111-111111111111","agent_seq":3,"event_type":"step_started","timestamp":"2026-11-05T08:00:01+00:00","data":{"step":"llm","round":0}}

id: 4
event: public_summary
data: {"schema_version":1,"run_uuid":"11111111-1111-4111-8111-111111111111","agent_seq":4,"event_type":"public_summary","timestamp":"2026-11-05T08:00:02+00:00","data":{"content":"支持轻量对话以及已配置的图片生成与编辑能力。"}}

: heartbeat

```

SSE 的每个事件以空行结束。`data` 是完整 EventEnvelope JSON，不是原始文本 token。

| EventEnvelope 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | integer | 当前为 1 |
| `run_uuid` | string | 执行标识 |
| `agent_seq` | integer | 从 1 开始的 Run 级单调递增序号，与 SSE id 一致 |
| `event_type` | string | 事件类型，与 SSE event 一致 |
| `timestamp` | string | 提交时间 |
| `data` | object | 当前事件的数据 |

### 7.3 事件类型

| event_type | 含义 | data 主要字段 |
|---|---|---|
| `run_accepted` | 执行已受理 | `status` |
| `run_started` | 开始执行 | `status` |
| `run_cancelled` | 迟到 StartRun 绑定已取消墓碑 | `status` |
| `action_preview` | 模型提出工具调用预告 | `tool_name`；预告不代表工具已执行 |
| `step_started` | LLM 或工具步骤开始 | LLM：`step`、`round`；工具：`invocation_id`、`tool_name`、`arguments` |
| `step_completed` | 步骤结束 | LLM：`step`、`round`；工具：`invocation_id`、`tool_name`、`success`、`artifacts` |
| `public_summary` | 可公开展示的模型回复文本 | `content`；不承诺逐 token 增量 |
| `interaction_requested` | 请求授权或补充输入 | pending_interaction 的字段 |
| `task_submitted` | 已取得外部 task_id | `invocation_id`、`tool_name`、`task_id`、`request_id`、`task_status` |
| `delivery_unknown` | 外部提交结果未确认 | `invocation_id`、`tool_name`、`remote_cancel_supported` |
| `artifact_committed` | 产物已提交 | `artifacts` |
| `command_receipt` | 控制命令回执或状态更新 | CommandReceipt 字段 |
| `run_terminal` | 正常结束、失败或本地取消 | `status`、`commands`、`artifacts` |
| `run_interrupted` | 重启恢复时记录中断 | `status` |

### 7.4 消费规则

1. 事件先持久提交，再发送给订阅方；客户端成功处理事件后保存 `agent_seq`。
2. 断流后使用最后已处理序号重新订阅；客户端按 `(run_uuid, agent_seq)` 去重。
3. 未提供游标会从 0 开始回放，可能再次收到历史事件。心跳是 SSE 注释，不占用事件序号。
4. 订阅断开不会取消 Run；停止执行必须调用 ControlRun。
5. 当前实现由持久提交唤醒订阅者，默认每 15 秒心跳，不做 250ms 全量读盘轮询。单条连接默认最长 540 秒，到时正常关闭，Run 继续；调用方按最后已处理序号重连。慢订阅者不阻塞提交，缓存淘汰时可从 journal 回放。
6. 对已终态 Run，发送当前游标后的已存事件后关闭连接。终态后新的控制请求仍可能提交回执；若需核对最新回执，使用 GetRun 或重新订阅，不依赖已关闭的连接。
7. 流已返回 200 后发生存储故障时会关闭连接，不能再返回普通 JSON 错误体，也不能仅凭 EOF 判定成功；应重新查询 Run。
8. 网关须关闭响应缓冲，设置合适的 SSE 空闲超时。响应携带 `Cache-Control: no-cache` 和 `X-Accel-Buffering: no`。

## 8. ControlRun：控制执行

### 8.1 请求参数

| 项目 | 值 |
|---|---|
| 方法 | POST |
| 路径 | `/v1/aigc/runs/{run_uuid}/commands` |
| operationId | `ControlRun` |
| 路径参数 | `run_uuid`：必填，UUID |
| 响应体 | CommandReceipt；拒绝时位于 ErrorResponse.details |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `command_id` | string | 是 | 安全命令标识，同一次命令重试保持不变 |
| `type` | string | 是 | `approve`、`skip`、`input`、`intervene`、`stop` |
| `interaction_id` | string / null | 条件必填 | approve/skip/input 必填；stop/intervene 不允许指定非 null 值 |
| `payload` | object | 否 | 默认 `{}`；按命令类型校验 |

| type | 适用场景 | payload / 行为 |
|---|---|---|
| `approve` | 当前 kind=approval 的交互 | payload 为空；只授权对应一次待执行工具调用 |
| `skip` | 当前 approval 或 input 交互 | payload 为空；跳过对应工具调用，不是取消整个 Run |
| `input` | 当前 kind=input 的交互 | 提交要补充/修正的参数对象，与 arguments 顶层合并后按 input_schema 校验 |
| `intervene` | 可控制的运行中执行 | 仅允许 `{"content":"用户指令"}`，长度 1～20,000；在下一个安全边界追加指令 |
| `stop` | 执行前、执行中或已终态 | payload 为空；取消本地执行，支持启动前取消墓碑 |

图片生成工具默认要求授权。收到 `action_preview` 不能直接推断已生成图片，只有对应步骤、外部 task_id 与产物提交才说明执行进度。

### 8.2 请求示例

以下命令体均发送到同一 `/commands` 路径；command_id 每个逻辑命令只生成一次。

授权：

```json
{"command_id":"approve-001","type":"approve","interaction_id":"22222222-2222-4222-8222-222222222222"}
```

跳过：

```json
{"command_id":"skip-001","type":"skip","interaction_id":"22222222-2222-4222-8222-222222222222"}
```

补充输入：

```json
{
  "command_id": "input-001",
  "type": "input",
  "interaction_id": "33333333-3333-4333-8333-333333333333",
  "payload": {"prompt": "生成一张戴生日帽的猫的图片"}
}
```

干预：

```json
{"command_id":"intervene-001","type":"intervene","payload":{"content":"后续图片改用水彩风格"}}
```

停止：

```bash
curl -X POST "${AIGC_BASE_URL}/v1/aigc/runs/11111111-1111-4111-8111-111111111111/commands" \
  -H "Authorization: Bearer ${AIGC_SERVICE_TOKEN}" \
  -H "X-User-Id: user-001" \
  -H "Content-Type: application/json" \
  -d '{"command_id":"stop-001","type":"stop"}'
```

### 8.3 回执字段与状态

| 字段 | 类型 | 说明 |
|---|---|---|
| `command_id` | string | 命令标识 |
| `type` | string | 命令类型 |
| `digest` | string | 命令内容摘要 |
| `status` | string | `accepted`、`applied`、`rejected`、`no_op` |
| `reason_code` | string | 接收、生效、拒绝或无操作的原因码 |
| `applied_agent_seq` | integer / null | 命令生效或最终处理的事件序号；尚未处理时为 null |
| `remote_cancel_supported` | boolean / null | stop 时当前为 false；其他命令通常为空 |
| `remote_task_may_continue` | boolean / null | stop 时按已有外部任务记录给出提示；不是远端实时查询结论 |

| 回执 status | HTTP | 含义 |
|---|---|---|
| `accepted` | 202 | 已收到，尚未确认生效 |
| `applied` | 200 | 已生效，例如启动前取消或重复查询已生效命令 |
| `no_op` | 200 | 无需再次动作，例如对终态 Run 调用 stop |
| `rejected` | 409 | 不满足执行条件，回执放在错误体 details 中 |

停止命令已接收示例：

```json
{
  "command_id": "stop-001",
  "type": "stop",
  "digest": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "status": "accepted",
  "reason_code": "ACCEPTED",
  "applied_agent_seq": null,
  "remote_cancel_supported": false,
  "remote_task_may_continue": true
}
```

交互过期示例，HTTP 409：

```json
{
  "code": "INTERACTION_EXPIRED",
  "message": "INTERACTION_EXPIRED",
  "request_id": "example-http-request-id",
  "details": {
    "command_id": "approve-001",
    "type": "approve",
    "digest": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    "status": "rejected",
    "reason_code": "INTERACTION_EXPIRED",
    "applied_agent_seq": null
  }
}
```

### 8.4 幂等与竞争处理

- 相同 command_id 和相同命令内容返回最新已存回执，不重复执行。修改类型、payload 或 interaction_id 需新 command_id，否则返回 `COMMAND_INPUT_CONFLICT`。
- 可通过重复 ControlRun、GetRun 的 `commands` 或 SSE 的 `command_receipt` 获取生效回执；不能将 accepted 当作 applied。
- 一个 interaction 只能被处理一次。迟到、已消费或类型不匹配的交互命令被拒绝；收到 INPUT_INVALID 后如修正参数，使用新的 command_id。
- stop 可先于 StartRun：持久保存 cancelled 墓碑，迟到 StartRun 绑定其输入后仍返回 cancelled，不重新激活执行。
- 对终态 Run 再次 stop 返回 no_op；其他新命令返回拒绝。此前已接收但未生效的非停止命令可在终态处理时变为 rejected。
- 停止与执行完成按持久台账提交顺序仲裁。stop 只能控制本地执行，不能撤销已经提交的外部生成任务。
- `remote_task_may_continue=false` 不能排除在途提交尚未拿到 task_id 的情况；下游没有取消保证时应继续按不确定副作用处理。

## 9. GetOrBuildRecipe：获取或构建配方

### 9.1 基本信息与请求参数

| 项目 | 值 |
|---|---|
| 方法 | POST |
| 路径 | `/v1/aigc/runs/{run_uuid}/recipe` |
| operationId | `GetOrBuildRecipe` |
| 路径参数 | `run_uuid`：必填，来源 Run UUID |
| 响应体 | RecipeView |

| 字段 | 类型 | 必填 | 约束 / 默认值 |
|---|---|---|---|
| `recipe_request_id` | string | 是 | 安全请求标识，作用域为当前用户 |
| `artifact_ids` | array<string> | 是 | 1～100 项，全部取自来源 Run 已提交的 artifacts；服务排序并去重 |
| `context` | string | 否 | 补充配方背景，最多 20,000 字符，默认空字符串 |

来源 Run 必须属于当前用户并已到达终态；所选产物必须归属该 Run，且用于构建的输入及执行上下文仍可读取。只有取消墓碑而无产物的 Run 不能构建配方。

### 9.2 请求示例

```bash
curl -X POST "${AIGC_BASE_URL}/v1/aigc/runs/11111111-1111-4111-8111-111111111111/recipe" \
  -H "Authorization: Bearer ${AIGC_SERVICE_TOKEN}" \
  -H "X-User-Id: user-001" \
  -H "Content-Type: application/json" \
  -d '{
    "recipe_request_id": "recipe-request-001",
    "artifact_ids": ["final_text"],
    "context": "将本次回答整理为可复用的能力介绍配方"
  }'
```

### 9.3 RecipeView 响应

| 字段 | 类型 | 说明 |
|---|---|---|
| `recipe_request_id` | string | 调用方请求标识 |
| `recipe_uuid` | string | 服务生成的配方标识 |
| `run_uuid` | string | 来源 Run |
| `status` | string | queued/running/succeeded/failed/interrupted |
| `result` | Recipe / null | 配方内容，构建成功时返回 |
| `error` | object / null | 构建失败或中断原因，包含 code、message |
| `created_at` | string | 受理时间 |
| `updated_at` | string | 最新提交时间 |

首次受理返回 202：

```json
{
  "recipe_request_id": "recipe-request-001",
  "recipe_uuid": "44444444-4444-4444-8444-444444444444",
  "run_uuid": "11111111-1111-4111-8111-111111111111",
  "status": "queued",
  "result": null,
  "error": null,
  "created_at": "2026-11-05T08:01:00+00:00",
  "updated_at": "2026-11-05T08:01:00+00:00"
}
```

没有独立的 Recipe GET 接口。调用方使用同一路径、同一 recipe_request_id、同一 artifact_ids/context 重复 POST 查询：queued/running 返回 202，succeeded/failed/interrupted 返回 200。

### 9.4 Recipe 内容

| 字段 | 类型 | 说明 |
|---|---|---|
| `recipe_uuid` | string | 配方 UUID |
| `version` | integer | 当前为 1；配方对象版本，不是自动修改的编辑版本 |
| `source` | object | `run_uuid`、`snapshot_digest`、`artifact_ids` |
| `prompt_template` | string | LLM 辅助提炼的提示词模板，使用 `{{variable_name}}` 占位符 |
| `parameters` | object | 以 invocation_id 为键，保存该调用实际工具参数；无已记录工具调用时为 `{}` |
| `variables` | array<object> | 模板变量定义 |
| `dependencies` | array<object> | 工具依赖 `{"type":"tool","name":"..."}` 和模型依赖 `{"type":"llm","model":"..."}` |
| `examples` | array<object> | `description` 与 `inputs` 组成的变量输入示例 |
| `artifact_refs` | array<object> | 选定产物的结构化引用，字段同 Artifact |
| `provenance` | object | `template_version`、`fact_digest`、`generated_at`，用于追溯来源事实 |

variables 中的每项：

| 字段 | 类型 | 必有 | 说明 |
|---|---|---|---|
| `name` | string | 是 | 唯一变量名，格式 `^[A-Za-z_][A-Za-z0-9_]{0,63}$` |
| `type` | string | 是 | string/integer/number/boolean/array/object |
| `description` | string | 是 | 变量含义，最多 2,000 字符 |
| `default` | 任意 JSON | 否 | 默认值，类型须匹配 type |
| `target` | object | 否 | `invocation_id` 和 `parameter`，只能绑定已记录调用中的实际参数 |

模板最多 20,000 字符，变量最多 50 个，示例最多 10 个。模板占位符和示例 inputs 的键只能使用已定义变量。来源、工具参数、依赖与产物引用由 SDK 根据事实填充，不由 LLM 自行指定。

配方 result 示例：

```json
{
  "recipe_uuid": "44444444-4444-4444-8444-444444444444",
  "version": 1,
  "source": {
    "run_uuid": "11111111-1111-4111-8111-111111111111",
    "snapshot_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "artifact_ids": ["final_text"]
  },
  "prompt_template": "面向 {{audience}}，介绍 AIGC 助手已配置的能力及使用边界。",
  "parameters": {},
  "variables": [{"name":"audience","type":"string","description":"目标读者","default":"业务开发者"}],
  "dependencies": [{"type":"llm","model":"qwen-plus"}],
  "examples": [{"description":"面向业务开发者","inputs":{"audience":"业务开发者"}}],
  "artifact_refs": [{
    "artifact_id": "final_text",
    "type": "text",
    "media_type": "text/plain",
    "content": "支持轻量对话以及已配置的图片生成与编辑能力。"
  }],
  "provenance": {
    "template_version": 1,
    "fact_digest": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "generated_at": "2026-11-05T08:01:10+00:00"
  }
}
```

### 9.5 构建与幂等规则

- 配方使用独立构建台账，不修改来源 Run 终态，也不在原 Run SSE 中输出配方生成进度。
- 同一用户的 recipe_request_id 绑定来源 Run、产物选择和 context；换来源或更改 context 时使用新 ID。
- artifact_ids 先排序去重，单纯调整选择顺序不会创建第二个构建。
- 已提交配方取原结果；失败或 interrupted 不隐式调用 LLM 重试，新构建使用新 recipe_request_id。
- LLM 超时、非法 JSON、schema 校验失败等记录为 failed；HTTP 查询仍可返回 200，必须看 RecipeView.status/error。
- 配方是可编辑 JSON，本服务没有配方编辑保存或直接执行配方的接口。不执行 dependencies，也不承诺相同模板能精确复现随机生成或已过期图片。

## 10. 错误说明

### 10.1 HTTP 业务错误结构

```json
{
  "code": "RUN_INPUT_CONFLICT",
  "message": "RUN_INPUT_CONFLICT",
  "request_id": "example-http-request-id",
  "details": null
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | string | 稳定的业务错误标识 |
| `message` | string | 当前 HTTP 错误处理通常返回与 code 相同的字符串 |
| `request_id` | string | 本次 HTTP 请求追踪 ID |
| `details` | 任意 JSON / null | 参数错误列表、命令拒绝回执等补充信息 |

字段校验失败时 details 为数组，例如 `[{"location":["body","run_uuid"],"type":"value_error"}]`；不包含完整原始请求或凭据。

以下为业务接口已定义的可预期错误。未知路径、错误方法、代理错误或未处理的内部异常，可能使用框架/网关默认响应，不能假定所有 404/405/500 都是上述结构。

### 10.2 HTTP 错误码

| HTTP | code | 含义 / 建议处理 |
|---|---|---|
| 400 | `USER_ID_INVALID` | 缺少或无效的 X-User-Id；由 backend 修正身份传递 |
| 400 / 422 | `INPUT_INVALID` | 通用参数、类型或字段校验失败；修正请求 |
| 400 | `EVENT_CURSOR_INVALID` | SSE header 游标不能解析，或游标不在合法范围 |
| 401 | `UNAUTHORIZED` | 缺少或错误的服务间 Bearer 凭据 |
| 404 | `RUN_NOT_FOUND` | Run 不存在或不属于当前用户 |
| 404 | `ARTIFACT_NOT_FOUND` | 选定产物不属于来源 Run，或不存在 |
| 409 | `RUN_INPUT_CONFLICT` | run_uuid 已绑定不同输入，不能更改原请求 |
| 409 | `COMMAND_INPUT_CONFLICT` | command_id 已绑定不同命令内容 |
| 409 | `RUN_NOT_CONTROLLABLE` | 当前 Run 不可执行此控制动作 |
| 409 | `INTERACTION_EXPIRED` | 无对应当前交互，或交互已变化 |
| 409 | `INTERACTION_CONSUMED` | 该交互已被其他命令处理 |
| 409 | `INTERACTION_TYPE_MISMATCH` | 命令类型与交互 kind 不一致 |
| 409 | `INPUT_INVALID` | input 命令合并后的工具参数不符合 input_schema；details 含拒绝回执 |
| 409 | `RECIPE_INPUT_CONFLICT` | recipe_request_id 已绑定不同构建输入 |
| 409 | `SOURCE_RUN_NOT_TERMINAL` | 来源 Run 尚未终态，稍后查询后再构建 |
| 413 | `REQUEST_TOO_LARGE` | JSON 请求体超出服务大小限制 |
| 429 | `RUN_QUEUE_FULL` | 全局或每用户容量已满，`details.scope=global|user`；遵守 Retry-After 后用原请求重试 |
| 429 | `EVENT_SUBSCRIBERS_FULL` | SSE 订阅容量已满，退避重连或改用 ETag 查询 |
| 429 | `RECIPE_QUEUE_FULL` | 配方在途构建容量已满，退避后使用原请求重试 |
| 503 | `SERVICE_UNAVAILABLE` | 服务当前不受理新执行/构建 |
| 503 | `STORAGE_UNAVAILABLE` | 本地台账不可读写；停止自动重提并联系运维 |
| 503 | `STORAGE_CORRUPTED` | 当前 Run/Recipe 的已提交日志损坏，不能清空原记录重新执行；其他健康记录仍可服务 |

`profile_id` 当前在请求 schema 中限制为 `aigc_assistant`，其他值通常返回 422。SDK 还定义了 profile 查找失败的 `PROFILE_NOT_FOUND`，不应以此代替 HTTP 字段校验。

### 10.3 命令 reason_code

| reason_code | 含义 |
|---|---|
| `ACCEPTED` | 已接收、尚未生效 |
| `APPLIED` | 已生效 |
| `CANCELLED_BEFORE_START` | 启动前取消已生效，不是 HTTP 错误码 |
| `RUN_TERMINAL` | 对终态 Run 停止无需动作，回执为 no_op |
| `RUN_TERMINATED` | 命令未生效前 Run 已终止，回执变为 rejected |
| `SERVICE_INTERRUPTED` | 服务恢复时未生效命令被中断 |
| `RUN_NOT_CONTROLLABLE` / `INTERACTION_EXPIRED` / `INTERACTION_CONSUMED` / `INTERACTION_TYPE_MISMATCH` / `INPUT_INVALID` | 对应拒绝原因，见 10.2 |

### 10.4 异步执行错误

下列 code 位于 RunView.error 或 RecipeView.error，不直接代表 HTTP 500。GetRun 或配方状态查询可以成功返回 200，但对象本身已经失败或中断。

| error.code | 对象 / 状态 | 说明 |
|---|---|---|
| `EXECUTION_FAILED` | Run / failed | 模型调用或执行过程失败 |
| `INTERACTION_TIMEOUT` | Run / failed | 等待授权或输入超过配置期限，未自动执行工具 |
| `EXTERNAL_TASK_UNRESOLVED` | Run / failed | 外部提交/查询结果未确认，停止继续规划，不自动重提 |
| `STORAGE_UNAVAILABLE` | Run / interrupted | 本地会话/记忆状态损坏或不可写，且台账仍能提交中断记录 |
| `SERVICE_INTERRUPTED` | Run/Recipe / interrupted | 服务中断或重启恢复，未自动续跑 |
| `RECIPE_BUILD_FAILED` | Recipe / failed | 配方 LLM 调用、解析或结构校验失败 |

若台账本身已无法写入，可能不能立即生成终态回执；应将 503/断流视作状态不确定并保留原幂等键，不能直接新建重复任务。

## 11. 推荐对接流程

1. backend 校验终端用户身份，固定生成 run_uuid 和 session_id，调用 StartRun。
2. 收到 202 后订阅 SSE；请求超时或回执丢失时调用 GetRun，必要时原 ID/原输入重试 StartRun。
3. 出现 waiting_input 时按 pending_interaction.kind 提交 approve、skip 或 input，保留 command_id。
4. 控制请求返回 accepted 后通过 GetRun、重复 ControlRun 或 SSE 核对最终回执。
5. Run 达到终态后读取 result/artifacts；存在 partial 或 requires_reconciliation 时按不确定执行处理。
6. 需要配方时选择已有 artifact_id，固定生成 recipe_request_id，重复相同 POST 直到配方终态。
7. 服务重启后继续使用原 ID 查询和回放；只有明确需要新业务执行/新构建时才生成新 ID。

建议采用有限退避轮询，避免高频读取全部台账；不要通过重复发送生成任务等待图片完成。

## 12. 健康检查与部署入口

| 方法 | 路径 | 认证 | 正常响应 | 非就绪响应 |
|---|---|---|---|---|
| GET | `/health/live` | 无 | 200 `{"status":"live"}` | 进程未提供服务时可能连接失败 |
| GET | `/health/ready` | 无 | 200 `{"status":"ready","config_digest":"..."}` | 503 `{"status":"unavailable","config_digest":"..."}` |

ready 检查运行服务是否接受任务、台账是否健康以及协调器是否持有文件租约，不发送收费模型请求，也不证明远端图片 API 可用。

- [统一服务配置](aigc/configs/service.yaml)：server、auth、storage、runtime、events、upstreams、agent、recipe、observability、eas、tools.api、secrets。图片工具 schema 和白名单也在该文件。
- 旧 `service/aigc/configs/api_tools.yaml` 已删除。只通过 `AIGC_CONFIG` 选择统一配置；`API_TOOLS_CONFIG`、`AIGC_QUEUE_LIMIT` 均在启动时拒绝。
- [Dockerfile](../docker/aigc.Dockerfile)、[Compose](../docker/aigc.compose.yaml)：Python 3.12、单协调器、非 root、只读根文件系统、本地数据卷。
- [中文启动说明](../README_ZH.md#aigc-轻量服务)：镜像构建、部署凭据与本地调试。

当前服务保持单进程、单协调器，不支持同一数据目录多副本写入。本地 POSIX 文件系统可用；NAS 必须验证 NFSv4 跨客户端 flock，生产锁策略 require 会拒绝 NFSv3/本地锁退化及对象存储/FUSE。epoch 仅作意外接管检测，不能替代互斥或提供分布式 fencing。存储故障会停止新副作用并使探针失败；日志损坏只隔离对应记录。

journal 是唯一事实来源，内存缓存只缓存已持久提交的状态和事件；缓存按条数/序列化大小限额淘汰，冷查询会读盘。热提交不重读 journal，常规事件做 journal 与目录各一次 fsync；受理、快照、终态投影和索引变更会有额外同步。活跃索引先于首次 journal 提交写入、终态提交后清理；首次建立恢复索引时仍需校验现有 journal，后续只恢复活跃项；这是持久化完整性检查，不提供旧配置或旧格式转换。不提供自动删除历史数据；清理必须另行授权，不能删除仍需保持幂等的记录。

## 13. 配置、单 Pod 调优与 EAS 部署

### 13.1 配置生效规则

唯一运行配置是 `service/aigc/configs/service.yaml`，由类型化 Settings 校验。所有环境覆盖在 YAML 的 `${VAR:default}` 中显式列出；凭据名由 `secrets` 声明，读取 `NAME` 或 `NAME_FILE`，凭据不进入冻结快照、部署 JSON 或日志。`AIGC_MAX_RUNS` 控制活跃槽，`AIGC_MAX_INFLIGHT` 控制总在途容量。旧配置字段、环境入口及程序化构造别名不再支持。

程序化使用 `Settings.load()` 加载配置，访问 `settings.server`、`settings.runtime` 等嵌套对象；不接受根层 `data_dir`、`max_runs`、`queue_limit`、`agent_config` 等旧字段。测试或嵌入调用的凭据使用 `ServiceCredentials` 单独注入，再通过 `settings.to_agent_config()` 派生 SDK 参数。服务入口不再合并 SDK 的另一份默认 YAML。

存储统一使用带缓存、活跃索引和批量持久化的新实现，不提供 `optimized=False` 双轨。轻量执行只使用显式注入的 LLM 客户端和 aiohttp API 工具，不回退到旧模型调用或 urllib；通用 SDK 的独立功能不属于服务配置入口。

修改配置后重启生效，不提供热更新。可通过 `/health/ready` 与受服务凭据保护的 `/internal/stats` 核对 `config_digest`。初始 64 活跃槽 / 32 LLM 并发不是压测保证，须按上游 RPM/TPM、响应时长、RSS 和 p95 排队时间校准。RPM=0 表示不做本地 RPM 限制，不表示远端无限额。上游连接池、IO 线程数、重试次数/总预算、交互超时、SSE 时长、keep-alive 和 drain 都集中在此文件。

LLM 对明确 429/502/503/504 及连接建立失败做有界重试；图片提交仅对明确 429 和未发出请求的连接建立失败重试，超时和 5xx 不重提。图片轮询在 poll_timeout 内容忍暂态 429/5xx/网络故障，按 Retry-After/退避等待；终态失败、非法状态或期限耗尽仍会失败。不能据此宣称 exactly-once。

### 13.2 EAS 部署流程

1. 使用 `docker/aigc.Dockerfile` 构建匹配目标 CPU 架构的镜像；常见 EAS x86 实例用 `--platform linux/amd64`。推送到同地域 ACR 内网仓库。应用端口 8000，避开平台的 8080/9090。
2. 在同一份 YAML 填写 `eas.image`、`eas.nas_server`、`eas.nas_path` 和网关类型；生产推荐同地域 VPC 的 MSE 专属网关，需填写 `eas.gateway_id`。ALB 的实际请求超时必须在网关配置和实测中确认，不能通过 rpc.keepalive 修改。
3. 在 NAS 预建 `shard-N`、`config`、`secrets` 子目录。数据目录给 UID/GID 10001 写权限；将该份 service.yaml 放入 `config/service.yaml`，凭据文件名分别为 secrets 中声明的环境变量名。配置和密钥目录只读挂载，与各分片可写目录分离。确认 NFSv4 跨客户端锁，不满足时不要绕过 require。
4. 生成部署 JSON（不创建云资源）：

   ```bash
   AIGC_CONFIG=/absolute/path/service.yaml python -m service.aigc.deploy eas --shard 0 --output eas-service.json
   ```

   用控制台自定义镜像部署或 eascmd 提交该 JSON。生成器固定 `instance=1`，使用 `metadata.rpc` 嵌套结构、探针、SIGTERM 和先停后起策略；不设置仅适用于 processor 的 worker_threads 或异步队列 max_queue_size。不依赖 Docker HEALTHCHECK 替代 EAS 探针。
5. 配置 VPC/安全组、ACR 内网拉取权限、到 DashScope/ModelScope 的 NAT 或私网出路。验证 DNS、TLS、SNAT 和远端账户配额；镜像已预装依赖，不在启动时安装。
6. backend 的调用地址为 `<EAS_ENDPOINT>/v1/aigc/...`。网关凭据用 `Authorization: <EAS_TOKEN>`，应用凭据用 `X-AIGC-Service-Token: <service-token>`，再传可信 `X-User-Id`。不要向终端用户泄露任一服务凭据。服务 links 和 /docs 使用配置的公开路径前缀。
7. 开启 EAS startup/readiness/liveness 探针。SIGTERM 到达时停止受理、关闭 SSE、取消排队或等待交互的 Run；执行中 Run 最多等待 drain_timeout，剩余任务中断并释放租约。控制命令在摘流量后不保证可达。SSE EOF 不表示 Run 成功，重连查询。

生成器只生成配置，没有执行云部署或平台验证。部署 JSON 中的资源、挂载和指标仍需真实 EAS 验收。先停后起更新会短暂不可用，backend 应保留原幂等键并退避重试，不能期待无损热升级。

### 13.3 压测与扩容操作指引

本地 mock 压测不访问收费上游：

```bash
python .dev_scripts/aigc/loadtest.py --users 10 --sessions 2 --runs 3 --image-ratio 0.3 --output baseline.json
```

压测报告包含成功吞吐、受理/执行时延、CPU/RSS、fsync 和 journal 读取计数。健康请求时延不是事件循环延迟；后者应读取 `/internal/stats` 的 loop_lag_ms_p99。生产吞吐必须用目标 Pod 规格和实际配额复测。EAS 平台验证脚本 `.dev_scripts/aigc/eas_probe.py` 的 serve/client 两种模式分别部署和调用，验证头透传、请求体、SSE、keep-alive、挂载与跨副本 flock；仅在隔离测试服务运行，报告不返回原始凭据。

先调单 Pod：分别观察排队、waiting_input、LLM/图片在途、RSS 和事件循环延迟，再调整活跃槽/IO 线程/连接池与限流；不能只看 HTTP QPS，因为 StartRun 的执行发生在 202 之后。LLM 的 token 消耗也受历史窗口影响。

人工横向扩容：生成 aigc_0、aigc_1 等独立 EAS 服务，每个固定一个 Pod、独立 NAS shard 子目录。backend 维护稳定 user→shard 表，新用户分配低负载分片，现有用户不因分片数量变化自动重映射。迁移时停止该用户所有 session 的新受理、等待在途任务清零、经授权迁移整个用户目录，再原子切换路由。禁止直接用 hash(user)%N 扩容重映射在途用户。

上游限流器是 Pod 内限流，不是模型账户的全局配额。多个分片共用凭据时，backend 需要统一控制账户 RPM/TPM，或给各分片静态分配 RPM 并预留余量；重启会清空本地速率窗口。当前不提供分布式 TPM 限流，不能把各 Pod 的配置直接乘以副本数当作账户可用吞吐。

不在同一服务启用多副本自动扩缩容；新增副本不会获得正在执行的内存控制对象。自动弹性需要单独批准的共享状态与执行认领设计，不能仅靠共享 NAS 或粘性路由。当前自定义指标仅用于监控与人工扩容决策。

平台事实参见官方[镜像部署](https://help.aliyun.com/zh/pai/deploy-a-model-service-by-using-a-custom-image)、[参数说明](https://help.aliyun.com/zh/pai/parameters-of-model-services)、[优雅退出](https://help.aliyun.com/zh/pai/scrolling-updates-with-graceful-exit)、[网关超时](https://help.aliyun.com/zh/pai/dedicated-gateway-timeout-best-practices)。