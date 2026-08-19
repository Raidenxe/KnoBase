# KnoBase RAG智能助手

基于 RAG（检索增强生成）的智能问答系统，面向产品说明书知识库，支持多路混合检索、防幻觉闭环、多租户权限、全链路追踪。

## 架构概览

```
用户请求 → FastAPI (REST/SSE/WS)
  ├── 认证层 (JWT + RBAC, 可选开启)
  ├── 全链路追踪 (TraceMiddleware)
  └── LangGraph 对话图
       ├── analyze   → 问题分析 / 指代消解
       ├── retrieve  → 多路混合检索 (向量 + BM25, RRF 融合)
       ├── grade     → 相关性精筛 + 词法重排
       ├── generate  → 受限生成 + 引用编号
       └── verify    → 一致性校验 → 防幻觉闭环
```

## 核心特性

| 模块 | 说明 |
|------|------|
| **多格式文档接入** | 支持 Markdown、TXT、PDF、DOCX、HTML 导入 |
| **智能分块** | 滑窗重叠分块，保留章节元数据与页码 |
| **混合检索 (BM25 + 向量)** | 独立关键词路 + 语义向量路，RRF 融合排序，BM25 失败自动降级 |
| **防幻觉闭环** | 无相关资料 → 拒答；校验失败 → 带反馈重试 → 仍失败 → 安全拒答 |
| **流式响应** | SSE 事件流 + WebSocket 双通道，边推理边返回 |
| **多轮对话** | SQLite 存储会话与消息，指代消解上下文 |
| **文档版本管理** | 版本历史、全文快照、一键回滚 |
| **多租户 RBAC** | JWT 认证，admin/owner/member/viewer 四角色，tenant 级数据隔离 |
| **全链路追踪** | 请求级 trace_id，5 阶段 span 耗时，P50/P95 指标汇总 |
| **接口性能监控** | 中间件埋点，按接口统计 P99 延迟与 QPS |
| **提示词热更新** | 在线编辑 System Prompt，保存即生效、无需重启 |
| **操作审计** | 敏感操作（上传/删除/导入/改配置）日志，责任可追溯 |
| **LLM 成本核算** | 记录每次真实调用 Token 消耗，估算费用 |
| **评估体系** | Hits/Precision/Recall/MRR@k，数据集驱动评估脚本 |
| **通知中心** | 铃铛入口 + 未读红点，工单动态 / 系统公告 / 知识库更新 / 权限变更自动通知；90 天自动保留 |
| **工单系统** | 用户提单与跟踪（附件/催办/确认关闭/重新打开）+ 管理端处理（状态流转/回复/统计仪表盘） |

## 快速开始

### 环境要求

- Python 3.10+
- 推荐使用虚拟环境

### 1. 安装依赖

```bash
git clone <repo-url> && cd rag-assistant
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 按需修改（默认 mock 模式可直接运行）
```

### 3. 准备知识库

将产品说明书（Markdown 文件）放入 `data/manuals/` 目录，启动时自动导入（`RAG_AUTO_INGEST_ON_STARTUP=true`）。

### 4. 启动服务

推荐使用一键脚本（自动加载 `.env`、后台运行、健康检查、PID 管理）：

```bash
./start.sh    # 启动
./stop.sh     # 停止
```

也可以直接以 uvicorn 前台运行（便于调试）：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

默认 `RAG_AUTH_MODE=off`（演示模式）可直接体验；生产建议 `RAG_AUTH_MODE=on` 并配置足够长的 `RAG_JWT_SECRET`。

首次运行将下载嵌入模型（`BAAI/bge-small-zh-v1.5`，约 100MB），请耐心等待。

### 5. 验证

```bash
# 健康检查
curl http://localhost:8000/api/v1/health

# 提问（SSE 流式）
curl -X POST http://localhost:8000/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"question": "网关如何恢复出厂设置？"}'

# 知识库浏览器
open http://localhost:8000/documents-browser
# 说明：Markdown 由 marked 渲染为排版文章；生产模式（RAG_AUTH_MODE=on）需先登录，
# 页面自动携带 JWT，401 时跳转登录页；聊天中的引用 [n] 也可跳转至此定位章节。
```

### 6. Web 前端

服务内置聊天界面，浏览器访问 `http://localhost:8000` 即可使用。

**演示模式（`RAG_AUTH_MODE=off`）**：直接进入聊天界面，无需登录。

**生产模式（`RAG_AUTH_MODE=on`）**：自动跳转登录页 `/login.html`，凭据校验通过后进入主页，页面自动在 API 请求中携带 JWT。侧边栏底部显示当前用户和角色，点击可退出。知识库浏览器（`/documents-browser`）与管理后台（`/admin.html`）同样携带 JWT，401 时自动跳转登录；分享页（`/share.html?token=...`）按链接有效期独立鉴权，无需账号。

## 后台管理与运维

监控中心 `http://<host>:8000/admin.html` 面向管理员，按 Tab 提供以下运维能力（生产模式下仅 `admin` 角色可见）：

| Tab | 说明 |
|-----|------|
| **全量对话** | 管理员浏览所有用户的对话，展示会话数/问题数/拒答率/反馈汇总 |
| **用户反馈** | 点赞/点踩统计与好评率 |
| **链路耗时** | 最近请求的 5 阶段 span 耗时（检索/重排/生成等） |
| **知识库** | 命中统计、文档批量删除、后台入库任务进度 |
| **接口性能** | 中间件埋点的各接口请求量/QPS/P50/P95/P99/峰值延迟 |
| **提示词** | 在线编辑生成 System Prompt，保存即生效、无需重启；可一键恢复内置默认 |
| **操作日志** | 敏感操作审计：谁在何时上传/删除/导入文档、修改提示词 |
| **资源水位** | 服务机器 CPU/内存/磁盘即时读数 + 24h 曲线（后台线程采样，落 `data/resource.db`） |
| **模型切换** | 运行时热更新 LLM 与 Embedding（保存即生效，无需重启）；维度变更提示重建集合 |
| **检索参数** | 运行时调整 Top-K / 相似度阈值 / RRF / Rerank 开关，下一请求即时生效 |
| **慢请求** | 全程超阈值的 `/api/*` 请求明细，点击可展开检索→重排→LLM 阶段耗时拆分 |
| **用户管理** | 用户/用户组/分类授权管理（RBAC 授权矩阵），登录日志与操作审计 |
| **工单管理** | 工单总览（状态/类型/紧急度筛选）、详情回复、状态流转、紧急高亮、催办标记与统计仪表盘 |
| **项目文档** | 在线浏览本项目 README 与 `docs/` 全部 Markdown 文档（admin） |

其中「接口性能」页同时展示 LLM 用量：近 N 小时调用次数、Token 消耗与估算费用（按模型分组）。

## 通知中心与工单系统

面向「内部工具、管理员统一管控」原则，内置**通知中心**与**简单工单系统**，二者均基于 SQLite 轻量落地、随事件自动触发、按租户隔离。

### 通知中心
- **入口**：主页右上角 🔔 铃铛（有未读时显示红点与数量），下拉显示最近 5 条；底部「查看全部通知」进入独立通知页。
- **类型**：工单动态（`ticket_status`/`ticket_reply`）、系统公告（`announce`）、知识库更新（`kb_update`）、权限变更（`permission`）；支持分类筛选。
- **操作**：单条标记已读 / 全部已读 / 单条或批量删除；点击跳转工单详情或知识库浏览器。
- **自动触发**：工单状态变更、管理员回复、文档上传/更新、分类授权调整均自动产生通知；管理端可手动广播系统维护公告（全体或指定用户组）。
- **保留策略**：默认保留最近 90 天（`RAG_NOTIFY_RETENTION_DAYS`），超期自动清理。

### 工单系统
- **用户端**（`/tickets.html` 或个人中心）：提交工单（类型/标题/描述/紧急度/附件≤10MB）、我的工单列表、详情 + 管理员回复时间线、状态流转消息、催办（每单限 2 次、满 3 个工作日未更新方可）、已解决确认关闭、7 天内重新打开补充说明。
- **管理端**（控制台「工单管理」Tab）：工单总览与组合筛选（状态/类型/紧急度）、详情回复（Markdown）、状态流转（待处理→处理中→已解决→已关闭，可填处理说明）、紧急工单红色高亮且优先排序、「⚠️ 已催办」标记、按类型/状态/平均处理时长的统计仪表盘。

相关数据落地文件：`data/notify.db`（用户通知）、`data/tickets.db`（工单与回复）。

其它持久化文件：`data/audit.db`（操作审计）、`data/llm_usage.db`（LLM 用量）、`data/prompts.json`（热更新提示词）、`data/resource.db`（资源水位采样）。接口性能采样本仅存于内存滑动窗口（`RAG_ENDPOINT_METRICS_TTL`，默认 300 秒）。

## API 一览

| 端点 | 方法 | 说明 | 认证 |
|------|------|------|------|
| `/api/v1/health` | GET | 健康检查 | 否 |
| `/api/v1/chat` | POST | 提问（非流式） | 可选 |
| `/api/v1/chat/stream` | POST | 流式问答 (SSE) | 可选 |
| `/api/v1/ws/chat` | WS | WebSocket 实时问答 | 可选 |
| `/api/v1/conversations` | GET | 会话列表 | 可选 |
| `/api/v1/conversations/{id}` | GET/DELETE | 会话详情/删除 | 可选 |
| `/api/v1/conversations/{id}/export` | GET | 导出会话（Markdown/Text） | 可选 |
| `/api/v1/conversations/{id}/export/share` | POST | 生成会话只读分享链接 | 可选 |
| `/api/v1/conversations/share/{token}` | DELETE | 撤销分享链接 | 可选 |
| `/api/v1/conversations/admin/stats` | GET | 对话监控聚合（全量对话/拒答率/反馈） | admin |
| `/api/v1/documents` | GET | 文档列表 | 可选 |
| `/api/v1/documents/import` | POST | 按路径批量导入/更新 | 可选 |
| `/api/v1/documents/upload` | POST | 上传文件导入 | 可选 |
| `/api/v1/documents/scan` | POST | 扫描目录批量导入（支持后台/增量） | 可选 |
| `/api/v1/documents/web-import` | POST | 从 URL 抓取文档导入 | 可选 |
| `/api/v1/documents/batch-delete` | POST | 批量删除文档 | 可选 |
| `/api/v1/documents/stats` | GET | 每文档命中/覆盖统计 | 可选 |
| `/api/v1/documents/{id}` | GET/DELETE | 文档详情/删除 | 可选 |
| `/api/v1/documents/{id}/content` | GET | 文档全文（供知识库浏览器渲染） | 可选 |
| `/api/v1/documents/{id}/versions` | GET | 版本历史 | 可选 |
| `/api/v1/documents/{id}/versions/{v}` | GET | 版本快照 | 可选 |
| `/api/v1/documents/{id}/versions/{v}/rollback` | POST | 回滚到指定版本 | 可选 |
| `/api/v1/tasks` | GET | 后台任务列表 | 可选 |
| `/api/v1/tasks/{task_id}` | GET | 任务状态与进度 | 可选 |
| `/api/v1/feedback` | POST | 提交/更新回答反馈（赞/踩） | 可选 |
| `/api/v1/feedback/stats` | GET | 反馈统计 | 可选 |
| `/api/v1/shares/{token}` | GET | 公开只读查看分享会话（JSON） | 否 |
| `/api/v1/share-html/{token}` | GET | 分享会话只读页面（HTML） | 否 |
| `/api/v1/traces` | GET | 最近请求链路列表 | 可选 |
| `/api/v1/traces/{trace_id}` | GET | 链路追踪详情 | 可选 |
| `/api/v1/traces/{trace_id}/detail` | GET | 单请求阶段拆分明细（慢请求分解） | 可选 |
| `/api/v1/metrics/basic` | GET | 汇总指标 | 可选 |
| `/api/v1/eval/summary` | GET | 评估摘要 | admin |
| `/api/v1/metrics/endpoints` | GET | 各接口 P99 延迟 / QPS（中间件埋点） | admin |
| `/api/v1/metrics/usage` | GET | LLM Token 消耗与成本核算 | admin |
| `/api/v1/admin/prompt` | GET/PUT | 查看 / 热更新生成 System Prompt | admin |
| `/api/v1/admin/models` | GET | 当前 LLM / Embedding 动态配置 | admin |
| `/api/v1/admin/models/llm` | PUT | 运行时切换 LLM 模型（立即生效） | admin |
| `/api/v1/admin/models/embedding` | PUT | 运行时切换 Embedding 模型（含维度变更提示） | admin |
| `/api/v1/admin/retrieval` | GET/PUT | 查看 / 热调整检索参数（Top-K/阈值/RRF/Rerank） | admin |
| `/api/v1/admin/resource/current` | GET | 当前资源水位（CPU/内存/磁盘） | admin |
| `/api/v1/admin/resource/series` | GET | 资源水位历史曲线（默认 24h） | admin |
| `/api/v1/admin/slow-requests` | GET | 最近慢请求列表 | admin |
| `/api/v1/audit` | GET | 敏感操作审计日志 | admin |
| `/api/v1/auth/login` | POST | 登录获取 JWT | auth on |
| `/api/v1/auth/me` | GET | 当前用户信息 | auth on |
| `/api/v1/auth/tenants` | GET | 租户列表 | auth on · admin |
| `/api/v1/auth/users` | GET/POST | 用户列表 / 创建用户 | auth on · owner/admin |
| `/api/v1/tickets` | GET/POST | 我的工单列表 / 提交工单 | 登录 |
| `/api/v1/tickets/{id}` | GET | 我的工单详情 | 登录 |
| `/api/v1/tickets/{id}/replies` | POST | 回复工单（管理员回复触发通知） | 登录 |
| `/api/v1/tickets/{id}/remind` | POST | 催办工单 | 登录 |
| `/api/v1/tickets/{id}/close` | POST | 确认关闭（提交人） | 登录 |
| `/api/v1/tickets/{id}/reopen` | POST | 重新打开（7 天内） | 登录 |
| `/api/v1/tickets/admin` | GET | 工单总览（组合筛选） | owner/admin |
| `/api/v1/tickets/admin/stats` | GET | 工单统计仪表盘 | owner/admin |
| `/api/v1/tickets/admin/{id}` | GET | 工单详情（管理端） | owner/admin |
| `/api/v1/tickets/admin/{id}/reply` | POST | 管理员回复 | owner/admin |
| `/api/v1/tickets/admin/{id}/status` | POST | 状态流转 | owner/admin |
| `/api/v1/notifications/recent` | GET | 下拉最近 N 条通知 + 未读数 | 登录 |
| `/api/v1/notifications/unread-count` | GET | 未读通知数 | 登录 |
| `/api/v1/notifications` | GET | 通知分页列表（按类型筛选） | 登录 |
| `/api/v1/notifications/{id}/read` | POST | 标记单条已读 | 登录 |
| `/api/v1/notifications/read-all` | POST | 全部已读 | 登录 |
| `/api/v1/notifications/delete-batch` | POST | 批量删除通知 | 登录 |
| `/api/v1/notifications/broadcast` | POST | 广播系统公告 / 知识库更新 | owner/admin |
| `/api/v1/project-docs` | GET | 项目文档列表（README + docs/*.md） | admin |
| `/api/v1/project-docs/{name}` | GET | 项目文档 Markdown 内容 | admin |

## 配置说明

所有配置通过环境变量 `RAG_<字段名>` 设置，详见 `.env.example`。

### 关键配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `RAG_LLM_PROVIDER` | `mock` | LLM 模式：`mock`（离线抽取式）/ `openai`（兼容 API） |
| `RAG_EMBEDDING_PROVIDER` | `fastembed` | 向量化：`fastembed`（本地 ONNX）/ `openai` |
| `RAG_MILVUS_URI` | `./data/milvus_lite.db` | Milvus 连接：文件路径（Lite）/ `http://host:19530`（服务） |
| `RAG_AUTH_MODE` | `off` | 认证模式：`off`（演示，默认）/ `on`（生产，启用 RBAC） |
| `RAG_RETRIEVAL_TOP_K` | `12` | 检索召回条数 |
| `RAG_HYBRID_BM25_K` | `12` | BM25 路召回条数 |
| `RAG_RRF_K` | `60` | RRF 融合常量 |
| `RAG_NOTIFY_DB_PATH` | `./data/notify.db` | 通知中心 SQLite 路径 |
| `RAG_NOTIFY_RETENTION_DAYS` | `90` | 通知自动保留天数（超期清理） |
| `RAG_TICKET_DB_PATH` | `./data/tickets.db` | 工单系统 SQLite 路径 |

### 生产模式 (RAG_AUTH_MODE=on)

```bash
RAG_AUTH_MODE=on
RAG_JWT_SECRET=<生成随机密钥>
RAG_DEFAULT_TENANT=my-company
```

首次启动后使用 CLI 创建管理员：

```bash
.venv/bin/python scripts/manage_users.py create-admin --username admin --password <密码>
```

随后浏览器访问 `http://<host>:8000` 将自动跳转登录页。

## 项目结构

```
rag-assistant/
├── app/
│   ├── api/routes/         # API 路由 (chat, documents, conversations, auth, ws, observability, tasks)
│   ├── core/               # 核心组件 (milvus_store, embeddings, llm, bm25, retrieval, tracing, security)
│   ├── graph/              # LangGraph 对话图 (nodes, state, edges)
│   ├── knowledge/          # 文档处理 (loader, splitter, pipeline)
│   ├── services/           # 数据服务 (history, auth_store, versions, trace_store, chat_service, metrics, audit, prompt_store, llm_usage, notify_store, ticket_store, doc_meta)
│   ├── eval/               # 评估体系 (registry)
│   ├── config.py           # 全局配置
│   └── main.py             # 应用入口
├── tests/                  # 单元测试
│   └── fixtures/manuals/   # 测试用说明书
├── scripts/                # 工具脚本 (migrate_schema, eval, manage_users)
├── eval/                   # 评估数据 (dataset.jsonl)
├── static/                 # 前端静态资源 (index.html, login.html, admin.html, browser.html, tickets.html)
├── docs/                   # 文档 (architecture, api, rbac, tracing, versioning, deployment, 增强方案-一期)
├── requirements.txt
├── .env.example
└── README.md
```

## 测试

```bash
# 运行全部测试
pytest tests/ -q

# 仅运行特定模块
pytest tests/test_hybrid.py -v
pytest tests/test_auth_rbac.py -v
pytest tests/test_eval_metrics.py -v
```

## 文档

- [架构概览](docs/architecture.md)
- [API 参考](docs/api.md)
- [RBAC 多租户管理](docs/rbac.md)
- [全链路追踪](docs/tracing.md)
- [文档版本管理](docs/versioning.md)
- [部署指南](docs/deployment.md)
- [增强实现方案（一期）与实施结果](docs/增强方案-一期.md)
- [通知中心与工单系统](docs/工单与通知.md)
- [运维四件套：模型切换 / 检索参数 / 资源监控 / 慢请求](docs/运维四件套-开发计划.md)

> 说明：上述项目文档均由控制台「项目文档」Tab 直接在线浏览（`README.md` 与 `docs/` 全量 Markdown）。

## 评估

```bash
# 运行评估脚本
python scripts/eval.py
```

评估指标：Hits@k · Precision@k · Recall@k · MRR@k，结果输出到 `eval/report_<ts>.json`。

## License

MIT