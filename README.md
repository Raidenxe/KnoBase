# KnoBase RAG 智能助手

基于企业知识库的 **检索增强生成（RAG）问答系统**。从多种格式文档（PDF / Word / TXT / Markdown）导入知识，通过 **向量检索 + BM25 关键词检索 + RRF 融合** 召回相关片段，生成**带来源引用**的回答，并内置多道防幻觉防线。

- 系统名称取自 *Knowledge Base*（知识库）。
- 核心思路：所有事实性回答严格依据知识原文，并为每个结论标注引用来源 `[1]`、`[2]`，点击可跳转到原始章节核对。

---

## 功能特性

**知识库管理**
- 单文件 / 批量上传，Web 网页抓取导入，目录（manuals）扫描导入
- 文档自动：解析标题结构 → 分块 → 向量化 → 入库
- 目录树浏览器，按章节浏览；文档在线编辑
- 文档可见权限（租户可见 / 私有）；按分类组织；批量删除
- 数据导出 CSV

**检索增强问答**
- 混合检索：语义向量（FastEmbed `bge-small-zh-v1.5`）+ BM25 + RRF 融合，可选 Rerank 精排
- 来源溯源：事实性结论后紧跟编号 `[n]`，点击跳转原始章节
- 多轮对话：基于会话上下文追问；会话可导出 / 分享 / 归档
- 多模态问答：支持附带图片提问
- 双重过滤 + 一致性校验等防幻觉机制，超范围时明确拒答

**身份与权限（RBAC，多租户）**
- 两种认证模式：
  - `off`：演示模式（默认），匿名单租户
  - `on`：生产模式，JWT 认证，角色（admin / owner / member / viewer）与租户隔离
- 首次启动提供「创建管理员 → 配置 LLM → 导入知识库」三步初始化向导

**平台能力**
- 管理控制台：概览统计、用户 / 角色、运行时配置热更新
- 可观测性：全链路追踪、接口级指标（QPS / 延迟）、资源水位监控、慢请求、审计日志
- 通知中心 + 工单系统；机器人接入（飞书 / 钉钉）异步问答
- 多语言（中文 / English）；移动端适配
- 交互式 OpenAPI 文档（`/docs`）

---

## 快速开始

### 方式一：Docker Compose（推荐）

默认单服务，内置 milvus-lite 嵌入式向量库，开箱即用：

```bash
docker compose up -d --build
```

访问 <http://localhost:8000>。

如需生产级独立 Milvus 集群：

```bash
docker compose --profile milvus up -d --build
# 并通过环境变量注入 RAG_MILVUS_URI=http://milvus:19530
```

数据目录 `./data` 通过命名卷持久化，容器重建不丢失。

### 方式二：本地运行

```bash
pip install -r requirements.txt
# 复制并修改配置
cp .env.example .env
# 启动
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> 首次启动会自动下载本地嵌入模型（`BAAI/bge-small-zh-v1.5`，仅需一次）。

### 提交知识

将文档放入 `./data/manuals/`（可含子目录作为分类），或通过前端「上传文档 / 网页导入」，系统会自动解析并入库。

### 开始提问

进入聊天页，输入如「KnoBase 的检索增强问答是如何实现的？」，系统返回带引用标注的回答。

---

## 配置说明

所有配置项通过环境变量注入，前缀 `RAG_`（参考 `.env.example`）。关键项：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RAG_LLM_PROVIDER` | `mock` | `mock` 离线演示 / `openai` 兼容 API（智谱 GLM / GPT / 通义等） |
| `RAG_LLM_API_KEY` | 空 | OpenAI 兼容接口的 Key，`mock` 下可留空 |
| `RAG_AUTH_MODE` | `off` | `off` 演示（匿名单租户）/ `on` 生产（JWT + RBAC + 租户隔离） |
| `RAG_JWT_SECRET` | 空 | 认证开启必填，JWT 签名密钥 |
| `RAG_EMBEDDING_PROVIDER` | `fastembed` | 本地 ONNX 模型（离线）/ `openai` 兼容 Embedding API |
| `RAG_MILVUS_URI` | `./data/milvus_lite.db` | 开发用嵌入式库；生产指向 `http://milvus:19530` |
| `RAG_RETRIEVAL_TOP_K` | `8` | 向量召回条数 |
| `RAG_HF_ENDPOINT` | 镜像地址 | 模型下载镜像，huggingface.co 不可达时使用 |

完整项见 [.env.example](.env.example)。

---

## 文档（Documentation）

仓库内维护面向公开的技术文档，见 `docs/` 目录：

| 文档 | 说明 |
| --- | --- |
| [docs/api.md](docs/api.md) | REST / WebSocket 接口参考 |
| [docs/architecture.md](docs/architecture.md) | 整体架构与技术选型 |
| [docs/deployment.md](docs/deployment.md) | 部署与配置（`RAG_*`）说明 |
| [docs/rbac.md](docs/rbac.md) | 认证与权限（RBAC / 租户隔离） |
| [docs/tracing.md](docs/tracing.md) | 全链路追踪 |
| [docs/versioning.md](docs/versioning.md) | 版本管理约定 |

> 公司与内部相关的规划、报告、评测数据、测试脚本、产品手册等**不作为公开文档随仓库发布**，仅保留在本机 / 内网。

---

## 安全

- 敏感配置（JWT 密钥、LLM / Embedding API Key）**一律通过环境变量注入**，严禁写入代码或提交 `.env`。
- `.env`、运行时数据目录 `data/`、测试脚本 `scripts/`、评测数据 `eval/`、内部文档等已通过 `.gitignore` 排除，不会进入版本库。
- 认证开启时，管理控制台仅管理员可访问；普通用户显示无权限提示而非部分界面。

---

## 版本历史

### v0.2.0
- 仓库公开化：仅保留公开技术文档，移除内部规划/报告、评测数据、测试脚本与产品手册
- 知识库目录树基于 Milvus 实际档案渲染，与库内文档一致
- 批量操作（批删 / 批改元数据）、CSV 导出、文档在线编辑
- 全部页面移动端适配；目录树与操作按钮无障碍 / 语义化改进
- 管理控制台按角色隔离，编辑权限动态渲染，展示真实角色
- Docker Compose 一键部署；认证模式统一为 `RAG_AUTH_MODE`
- Web 爬虫导入前端化；多语言；飞书 / 钉钉机器人接入；多模态问答
- 平台更名 **KnoBase RAG 智能助手**

### v0.1.0
- 首个可用版本：知识库导入、混合检索问答、来源引用溯源、多轮会话管理、RBAC 多租户、管理控制台、可观测性

---

## License

私有项目，保留所有权利。