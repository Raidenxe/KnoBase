# API 参考

Base URL: `http://<host>:8000`，交互式文档：`/docs`（Swagger UI）。

所有接口均返回 JSON（除 `/chat/stream` 为 SSE）。时间字段为 ISO-8601 UTC。

---

## 1. 问答

### POST `/api/v1/chat` — 提问（非流式）

请求：

```json
{
  "question": "SmartOps 平台默认管理员账号和初始密码是什么？",
  "conversation_id": null
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| question | string(1-2000) | 是 | 用户问题 |
| conversation_id | string | 否 | 多轮对话时传入；缺省新建会话 |

响应（200）：

```json
{
  "conversation_id": "546af975871e4e5e",
  "answer": "根据《智慧运维管理平台产品说明书》等说明书资料, 为您整理如下:\n\n1. 系统默认管理员账号为 admin，初始密码 Admin@2024。 [1]\n...",
  "citations": [
    {
      "index": 1,
      "chunk_id": "8f663693ad3943cd_00006",
      "doc_id": "8f663693ad3943cd",
      "doc_name": "智慧运维管理平台产品说明书",
      "section_path": "智慧运维管理平台产品说明书 > 3 安装部署 > 3.3 默认账号",
      "page": -1,
      "score": 0.6841,
      "snippet": "系统默认管理员账号为 admin，初始密码 Admin@2024。首次登录时系统强制要求修改初始密码…"
    }
  ],
  "verified": true,
  "metrics": { "retrieval_ms": 86, "generation_ms": 531, "verify_ms": 0, "total_ms": 669 },
  "refusal_reason": ""
}
```

| 字段 | 说明 |
| --- | --- |
| answer | 最终回答；拒答时为标准拒答话术 |
| citations | 回答中实际引用的来源列表（`index` 对应正文 `[n]` 标记）；拒答时为空数组 |
| verified | 是否通过事实一致性校验 |
| metrics | 各阶段耗时（毫秒） |
| refusal_reason | 拒答原因（未拒答为空串） |

### POST `/api/v1/chat/stream` — 提问（SSE 流式）

请求体同上。响应 `Content-Type: text/event-stream`，事件序列：

```
event: status
data: {"stage": "retrieve", "message": "正在检索产品说明书..."}

event: status
data: {"stage": "retrieved", "message": "召回 6 个相关片段", "retrieval_ms": 146}

event: token
data: {"content": "根据《DataGate数据采集网关产品说明书》等说明书资料, ...\n"}

event: token
data: {"content": "1. 长按设备 RESET 按键 8 秒以上… [1]\n"}

event: done
data: {"conversation_id": "...", "answer": "...", "citations": [...], "verified": true, "metrics": {...}, "refusal_reason": ""}
```

| 事件 | 说明 | 客户端处理建议 |
| --- | --- | --- |
| `status` | 阶段提示（analyze/retrieve/retrieved/grade/generate/verify） | 展示进度条 |
| `token` | 增量文本 | 追加渲染（打字机） |
| `reset` | 校验失败重试，旧答案作废 | 清空当前气泡重新接收 |
| `done` | 最终权威结果（含引用/指标） | 用 answer 覆盖本地拼接文本，渲染引用卡片 |
| `error` | 异常 | 展示错误 |

> 消费示例见 `static/index.html`（fetch + ReadableStream 解析）。

curl 示例：

```bash
curl -N -X POST http://localhost:8000/api/v1/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"question": "网关 ERR 灯常亮怎么办？"}'
```

---

## 2. 知识库管理

### GET `/api/v1/documents` — 文档清单

```json
{
  "documents": [
    { "doc_id": "8f663693ad3943cd", "doc_name": "智慧运维管理平台产品说明书", "chunks": 14, "created_at": 1786963065 }
  ],
  "total": 3, "total_chunks": 40
}
```

### POST `/api/v1/documents/import` — 按路径批量导入/更新

```json
{ "paths": ["/data/manuals/新说明书.md", "/data/manuals/v2/产品手册.pdf"] }
```

响应：`{ "imported": [{doc_id, doc_name, chunks, duration_ms}], "errors": [], "imported_count": 2, "error_count": 0 }`
同名文档自动覆盖旧版本（幂等更新）。

### POST `/api/v1/documents/upload` — 上传文件导入

`multipart/form-data`，字段名 `files`（可多文件），支持 `.md .txt .pdf .docx`。

```bash
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -F "files=@新产品说明书.md"
```

### POST `/api/v1/documents/scan` — 扫描目录批量导入

```json
{ "directory": "/data/manuals" }     // 缺省使用 RAG_MANUALS_DIR
```

Query 参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `background` | `false` | `true` 时后台异步执行，立即返回 `task_id`，不阻塞服务 |
| `incremental` | `true` | 增量模式：指纹（名称+大小+mtime）未变化的文档自动跳过，避免重复向量化 |

后台模式响应：

```json
{ "task_id": "76036b6713f2", "status": "running", "progress_url": "/api/v1/tasks/76036b6713f2" }
```

### POST `/api/v1/documents/web-import` — 从互联网 URL 导入文档

抓取网页 → 智能抽取正文（去除导航/广告噪音）→ 转为 Markdown 入库，用于公开技术文档/官方文档对私有知识库的补充。导入后与本地文档完全同权（参与检索、携带引用、受防幻觉校验约束）。

```json
{ "urls": ["https://milvus.io/docs/overview.md"], "background": false }
```

### GET `/api/v1/tasks/{task_id}` — 查询后台任务进度

```json
{
  "id": "76036b6713f2", "type": "scan", "status": "done",
  "progress": { "done": 3, "total": 55, "current": "ACP大模型c3笔记" },
  "result": { "imported_count": 3 }, "errors": []
}
```

> 任务记录为进程内存态，服务重启后查询将返回 404（已写入的向量数据不受影响）。

### GET `/api/v1/documents/{doc_id}/content` — 文档全文（知识库浏览器渲染）

```json
{ "doc_id": "8f663693ad3943cd", "doc_name": "智慧运维管理平台产品说明书", "content": "## 概述\n...", "chunk_count": 14 }
```

### POST `/api/v1/documents/batch-delete` — 批量删除文档

```json
{ "doc_ids": ["8f663693ad3943cd", "4b17a9920c5e3e7f"] }
```

### DELETE `/api/v1/documents/{doc_id}` — 删除文档及全部向量

---

## 3. 会话管理

| 端点 | 说明 |
| --- | --- |
| GET `/api/v1/conversations?limit=50` | 会话列表（含 message_count，按更新时间倒序） |
| GET `/api/v1/conversations/{id}` | 会话详情 + 全部消息（assistant 消息携带 citations 数组） |
| GET `/api/v1/conversations/{id}/export?format=markdown` | 导出会话（`markdown`/`text`），返回文本或文件下载 |
| POST `/api/v1/conversations/{id}/export/share` | 生成会话只读分享链接，返回 `{ "url": "/share.html?token=..." }`；接收方无需登录即可查看快照 |
| DELETE `/api/v1/conversations/share/{token}` | 撤销指定分享链接 |
| DELETE `/api/v1/conversations/{id}` | 删除会话及其消息（404 若不存在） |

> 会话分享另有两类公开读取端点：`GET /api/v1/shares/{token}`（JSON）、`GET /api/v1/share-html/{token}`（HTML 只读页），均无鉴权、按链接有效期独立校验。

### GET `/api/v1/conversations/admin/stats` — 对话监控聚合（仅 admin）

管理员视角的全量统计：所有用户会话数、总问题数、拒答率、反馈汇总与近期链路分布，供后台「全量对话」Tab 展示。

### POST `/api/v1/feedback` · GET `/api/v1/feedback/stats` — 回答反馈

| 端点 | 说明 |
| --- | --- |
| POST `/api/v1/feedback` | 提交/更新一条反馈，`{ "conversation_id": "...", "message_id": "...", "rating": "up"\|"down", "comment": "" }` |
| GET `/api/v1/feedback/stats` | 点赞/点踩统计与好评率（后台「用户反馈」Tab） |

---

## 4. 系统

### GET `/api/v1/health` — 健康检查

```json
{
  "status": "healthy",
  "version": "1.0.0",
  "components": {
    "milvus":     { "status": "up", "chunks": 40 },
    "embedding":  { "status": "up", "provider": "fastembed", "dimension": 512 },
    "llm":        { "status": "up", "provider": "mock" }
  }
}
```

### GET `/` — Web 聊天界面

---

## 5. 认证（生产模式 RAG_AUTH_MODE=on）

| 端点 | 说明 |
| --- | --- |
| POST `/api/v1/auth/login` | 账号密码换取 JWT；返回 `{ "access_token": "...", "user": {...} }` |
| GET `/api/v1/auth/me` | 当前用户信息（携带 `Authorization: Bearer <token>`）；无效/过期返回 401 |

除 `/api/v1/health`、`/auth/login`、`/share` 相关外，其余接口需携带 Bearer Token；`admin` 独有接口见下节。WebSocket 鉴权通过 query `?token=<token>` 传入。

## 5.5 通知中心与工单系统

面向「内部工具、管理员统一管控」原则。通知基于 `data/notify.db`（SQLite，90 天自动保留），工单基于 `data/tickets.db`。

### 工单（用户端 `/api/v1/tickets`）

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/api/v1/tickets` | GET | 我的工单列表（分页 / 状态 / 类型筛选） |
| `/api/v1/tickets` | POST | 提交工单（`multipart/form-data`：`ticket_type`/`title`/`description`/`urgency`/`attachment`?） |
| `/api/v1/tickets/{id}` | GET | 我的工单详情（含回复时间线） |
| `/api/v1/tickets/{id}/replies` | POST | 回复工单 `{ "content" }`（管理员回复触发通知） |
| `/api/v1/tickets/{id}/remind` | POST | 催办工单（满 3 个工作日未更新且每单限 2 次） |
| `/api/v1/tickets/{id}/close` | POST | 确认关闭（仅提交人，已解决后可关） |
| `/api/v1/tickets/{id}/reopen` | POST | 重新打开（7 天内，并可补描述） |

提交成功响应：`{ "id": "TK20260819001", "status": "pending", "message": "已提交，管理员将在 1-2 个工作日内处理", "link": "/tickets.html?tk=..." }`

### 工单（管理端 `/api/v1/tickets/admin`，owner/admin）

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/api/v1/tickets/admin` | GET | 工单总览（`status`/`ticket_type`/`urgent` 组合筛选），返回 `reminded` 标记已催办工单 |
| `/api/v1/tickets/admin/stats` | GET | 统计仪表盘：按状态/类型分布、平均处理时长、近期趋势 |
| `/api/v1/tickets/admin/{id}` | GET | 工单详情（管理端） |
| `/api/v1/tickets/admin/{id}/reply` | POST | 管理员回复 `{ "content" }` → 用户端实时通知 |
| `/api/v1/tickets/admin/{id}/status` | POST | 状态流转 `{ "status": "processing"|"resolved"|"closed", "note"?: "" }` |

### 通知（`/api/v1/notifications`）

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/api/v1/notifications/recent?limit=5` | GET | 下拉面板最近 N 条通知 + `unread` 未读数 |
| `/api/v1/notifications/unread-count` | GET | 未读通知数 |
| `/api/v1/notifications?ntype=&limit=&offset=` | GET | 通知分页列表，`ntype` 可选 `ticket_status`/`ticket_reply`/`announce`/`kb_update`/`permission` |
| `/api/v1/notifications/{id}/read` | POST | 标记单条已读 |
| `/api/v1/notifications/read-all` | POST | 全部已读 |
| `/api/v1/notifications/delete-batch` | POST | 批量删除 `{ "ids": [...] }` |
| `/api/v1/notifications/{id}` | DELETE | 删除单条通知 |
| `/api/v1/notifications/broadcast` | POST | 广播公告 / 知识库更新（owner/admin）`{ "ntype": "announce"|"kb_update", "title": "...", "content": "...", "group_ids"?: [...] }` |

### 项目文档（`/api/v1/project-docs`，admin）

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/api/v1/project-docs` | GET | 列出 `README.md` + `docs/*.md` 全部项目文档（名称/大小/修改时间） |
| `/api/v1/project-docs/{name}` | GET | 返回指定文档 Markdown 原文 `{ "name": "...", "content": "..." }`，供控制台「项目文档」Tab 渲染 |

> 自动通知触发点：工单状态流转 / 管理员回复 → 通知提交人；文档上传 / 分类授权调整 → 通知相关用户；管理端 `broadcast` → 全体或指定用户组。

## 6. 运维观测

| 端点 | 方法 | 认证 | 说明 |
| --- | --- | --- | --- |
| `/api/v1/metrics/basic` | GET | 可选 | 分阶段 P50/P95/峰值 与 拒答率/失败率汇总 |
| `/api/v1/traces` | GET | 可选 | 最近请求链路列表（按 trace_id 反序） |
| `/api/v1/traces/{trace_id}` | GET | 可选 | 单次请求的全链路阶段耗时详情 |
| `/api/v1/metrics/endpoints` | GET | admin | 中间件埋点：各接口请求量 / QPS / P50 / P95 / P99 / 峰值延迟（滑动窗口 `RAG_ENDPOINT_METRICS_TTL`，默认 300s） |
| `/api/v1/metrics/usage` | GET | admin | LLM Token 消耗与成本估算（按模型/provider 分组，`?hours=` 时段） |
| `/api/v1/admin/prompt` | GET | admin | 查看当前生效的生成 System Prompt（含是否自定义） |
| `/api/v1/admin/prompt` | PUT | admin | 热更新 System Prompt，保存即生效、无需重启；`{ "reset": true }` 恢复内置默认 |
| `/api/v1/audit` | GET | admin | 敏感操作审计日志（上传/删除/导入/回滚/改提示词：谁·何时·做了什么） |
| `/api/v1/eval/summary` | GET | admin | 最近一次评估摘要 |

数据落地：`data/audit.db`（审计）、`data/llm_usage.db`（LLM 用量）、`data/prompts.json`（热更新提示词）。接口性能采样本仅存于内存（无落盘）。

## 7. 错误约定

| 状态码 | 场景 |
| --- | --- |
| 422 | 请求参数校验失败（question 为空/超长等） |
| 400 | 导入路径为空 / 上传文件类型不支持 |
| 404 | 会话或目录不存在、文档删除目标不存在 |
| 500 | 内部错误（SSE 模式以 `error` 事件返回） |
