# 架构设计

## 1. 总体架构

```
┌──────────────┐     ┌─────────────────────────────────────────────┐
│  Web UI /    │     │              FastAPI 应用层                  │
│  REST 客户端  │────▶│  /chat  /chat/stream(SSE)  /documents       │
└──────────────┘     │  /conversations  /health                    │
                     └───────────────┬─────────────────────────────┘
                                     │ ChatService 编排
                     ┌───────────────▼─────────────────────────────┐
                     │        LangGraph 对话图 (Corrective RAG)     │
                     │                                             │
                     │  analyze → retrieve → grade ──┬→ generate   │
                     │    (改写)   (向量检索) (相关性精筛)│     ↓      │
                     │                              │   verify     │
                     │              无相关资料→refuse │  ↓    ↑      │
                     │  refuse(安全拒答)◀──重试耗尽────┘  重试│      │
                     └───────┬───────────────────────┬─────────────┘
                             │                       │
                  ┌──────────▼─────────┐   ┌─────────▼──────────────┐
                  │   Milvus 向量库     │   │  LLM Service           │
                  │  HNSW / COSINE     │   │  openai 兼容 / mock    │
                  │  (lite 或 服务端)   │   │  + Embedding(fastembed)│
                  └──────────▲─────────┘   └────────────────────────┘
                             │ 入库
                  ┌──────────┴───────────────────┐
                  │ 知识流水线: loader → splitter │
                  │ (md/txt/pdf/docx)  (章节分块) │
                  └──────────────────────────────┘
```

## 2. 知识处理流水线

### 2.1 加载（app/knowledge/loader.py）

不同格式统一解析为 `TextBlock(text, section_path, page)`：

| 格式 | 解析方式 | 引用元数据 |
| --- | --- | --- |
| Markdown | 按 `#`/`##`/`###` 层级切分，代码块整体保留 | 章节路径，如 `3 安装部署 > 3.3 默认账号` |
| PDF | pypdf 逐页提取 | 页码 |
| docx | 按 Heading 样式构建章节树 | 章节路径 |
| txt | 按空行分段 | `正文` |

### 2.2 分块（app/knowledge/splitter.py）

- 目标块长 `chunk_size=480` 字符（中文），相邻块重叠 `overlap=64`，保证跨块语义连续
- 超长块按字符滑窗切分，丢弃过短尾部碎屑
- **块与章节元数据绑定**：每个 chunk 携带 `doc_name / section_path / page`，是来源引用的基础

### 2.3 向量化与入库（app/knowledge/pipeline.py）

- 嵌入模型：`BAAI/bge-small-zh-v1.5`（512 维，中文检索优化，ONNX 本地推理，离线可用）；可切换 OpenAI 兼容 API 或降级 Hash 嵌入
- `doc_id = sha1(文档名+大小+mtime)[:16]`，chunk 主键 `doc_id_序号`
- 入库幂等：同名文档先删后插，天然支持**批量导入与更新**
- **增量导入**：`incremental=True` 时按指纹预取一次已入库集合，未变化文档整体跳过（55 文档重启从 ~2 分钟降到 <1 秒）
- **不阻塞服务**：启动自动导入与 `/scan?background=true` 均在后台线程执行，通过 `GET /api/v1/tasks/{id}` 查询 `done/total/current` 进度
- **互联网知识补充**：`/documents/web-import` 抓取 URL → 正文降噪（剥离导航/广告/菜单短行、去重）→ 转存 Markdown 入库，与本地文档同权受统一校验

> 嵌入语言提示：bge-small-zh 为中文优化模型，**中文提问检索英文文档**时跨语言相似度偏低（可能拒答）。中英混合知识库建议将 `RAG_FASTEMBED_MODEL` 切换为多语言模型（如 `BAAI/bge-m3`）并全量重建索引，或使提问语言与文档语言一致。

### 2.4 Milvus Schema

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| chunk_id | VARCHAR(64) PK | 块唯一标识 |
| vector | FLOAT_VECTOR(512) | HNSW 索引，COSINE 度量 |
| text | VARCHAR(8192) | 块原文 |
| doc_id / doc_name | VARCHAR | 所属文档 |
| section_path | VARCHAR(1024) | 章节路径（引用定位） |
| chunk_index / page / created_at | INT64 | 序号 / PDF 页码 / 时间戳 |

## 3. LangGraph 对话图（防幻觉闭环）

状态对象 `GraphState` 在节点间流转，包含问题、历史、检索结果、上下文块、回答、引用、校验结果、重试计数与各阶段耗时指标。

### 3.1 节点职责

| 节点 | 职责 | 防幻觉作用 |
| --- | --- | --- |
| `analyze_query` | 结合多轮历史做指代消解，生成独立检索问题 | 提升"问对检索"的准确率 |
| `retrieve` | **多路混合检索**：向量路（Milvus COSINE 超采样 2×top_k + 阈值过滤）与独立 BM25 关键词路并行召回 → RRF 融合评分（`RAG_RRF_K`），BM25 不可用自动降级为纯向量；可选手 `bge-reranker` 精排 | 第一层过滤；语义 + 词法互补，同领域大库中避免精确片段被泛领域块淹没 |
| `grade_documents` | 对每个候选块做相关性精筛（LLM 判定 / 关键词覆盖率+分数启发式），全不合格 → 路由至 `refuse` | **第一道闸**：无依据绝不生成 |
| `generate` | 受限上下文生成，系统提示强制"只依据资料 + 标注 [n] 编号"；token 实时 SSE 推送 | 生成端约束 + 引用强制 |
| `verify` | 逐句审核回答与资料一致性（LLM JSON 审核，审核上下文含文档名/章节元数据；离线回退：逐字包含 ∨ bigram 覆盖率≥0.45 双通道） | **第二道闸**：不一致 → 带反馈重试 → 仍失败 → 拒答 |
| `refuse` | 输出标准拒答话术与人工支持渠道 | 兜底：宁可不答，不可乱答 |

### 3.2 条件路由

```
grade_documents ── filtered 为空 ──▶ refuse
              └── 有相关块 ────────▶ generate
verify ── supported ────────────────▶ END
      └── 失败 & retries ≤ max ────▶ generate (携带审核反馈, 前端收到 reset 事件清空旧答案)
      └── 失败 & 重试耗尽 ──────────▶ refuse
```

### 3.3 流式实现

图节点通过 `state["emit"]` 异步回调将事件写入 `asyncio.Queue`，`ChatService.chat_stream` 边执行图边消费队列，形成 SSE 事件流：

| 事件 | 时机 | 负载 |
| --- | --- | --- |
| `status` | 各阶段开始/完成 | `{stage, message, retrieval_ms?}` |
| `token` | 生成过程中 | `{content}` |
| `reset` | 校验失败触发重试 | `{reason}`（前端清空旧答案） |
| `done` | 图执行完毕 | 最终权威回答 + 引用 + 指标 |
| `error` | 异常 | `{detail}` |

## 4. 防幻觉策略详解

1. **检索结果过滤**：COSINE 分数 < 0.42 的召回直接丢弃
2. **相关性双判定**：向量分数之外叠加关键词 bigram 覆盖率（≥0.10）或 LLM 二分类，避免"向量相似但主题无关"的误召回
3. **受限生成**：系统提示明确禁止使用资料外知识；命令/参数/数值照抄原文
4. **引用强制**：无 `[n]` 编号的回答无法通过校验（视为无依据陈述）
5. **一致性校验（第二道闸）**：
   - LLM 模式：审核员模型逐句核对，输出 `{supported, unsupported[]}` JSON
   - 离线模式：归一化原文包含式校验（陈述必须能在被引块中找到原文依据）
6. **失败处理**：首次失败携带审核反馈重试；重试耗尽则安全拒答，**未通过校验的内容不会送达用户**
7. **离线抽取式模式（mock）**：直接摘录说明书原句作为回答，构造性零幻觉，用于无 API Key 环境的演示与测试

## 5. 多轮对话

- 会话与消息持久化于 SQLite（`conversations` / `messages` 表），支持列表、详情、删除
- 每轮携带最近 8 条历史；`analyze_query` 将"那它的端口是多少？"改写为独立问题后再检索，避免指代词污染向量
- 助手消息连同引用 JSON 一并存档，历史回看时引用卡片完整还原

## 6. 性能设计

- 嵌入推理与 Milvus 检索放入线程池（`asyncio.to_thread`），不阻塞事件循环，保证并发吞吐
- Milvus HNSW（M=16, efConstruction=200）+ 本地 ONNX 嵌入 → 检索 p95 ~117ms
- 全链路指标（retrieval_ms / generation_ms / verify_ms / total_ms）随每次回答返回，可直接接入监控

## 7. 通知中心与工单系统

面向「内部工具、管理员统一管控」原则，内置**通知中心**与**简单工单系统**。二者均为轻量 SQLite 落地、随事件自动触发、按租户隔离，延续项目「零迁移」惯例。

### 7.1 存储层

```
data/notify.db   notifications(user_id, type, is_read, created_at)   ← 90 天自动清理
data/tickets.db  tickets(...) + ticket_replies(ticket_id, is_admin)  ← 工单与管理员回复时间线
```

- 每条记录携带 `tenant_id`，查询必须携带当前租户；工单按 `creator_id` 隔离，回复可区分用户/管理员。
- 通知保留策略 `RAG_NOTIFY_RETENTION_DAYS`（默认 90），由定长清理逻辑超期删除。

### 7.2 通知类型与触发

| 通知类型 | 触发事件 | 接收人 | 来源模块 |
| --- | --- | --- | --- |
| `ticket_status` | 工单状态变更 | 提交人 | tickets.py |
| `ticket_reply` | 管理员新增回复 | 提交人 | tickets.py |
| `kb_update` | 文档上传 / 更新 | 全租户或指定组 | documents.py |
| `announce` | 管理端手动广播公告 | 全租户或指定组 | notifications.py |
| `permission` | 分类授权调整 | 该组被影响的成员 | categories.py |

### 7.3 工单状态机

```
pending(待处理) → processing(处理中) → resolved(已解决) → closed(已关闭)
      ↑______________________ 7 日内 reopen ______________│
```

- 用户端：提交（类型/标题/描述/紧急度/附件≤10MB）、催办（满 3 个工作日未更新且每单限 2 次）、已解决确认关闭、7 日内重新打开。
- 管理端：总览组合筛选（状态/类型/紧急度）、详情回复、手动流转（可填处理说明）、紧急高亮优先、「⚠️ 已催办」标记、统计仪表盘。
