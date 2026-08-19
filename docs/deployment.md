# 部署运维

## 1. 部署形态

| 形态 | Milvus | 适用场景 | 启动方式 |
| --- | --- | --- | --- |
| 开发/演示 | milvus-lite 嵌入式（单文件） | 本机、PoC、CI 测试 | `uvicorn app.main:app` |
| 生产 | Milvus Standandalone（Docker） | 团队/线上服务 | `docker compose up -d` |
| 生产扩展 | Milvus Cluster | 大规模知识库（千万级向量） | 参照 Milvus 官方集群方案，仅改 `RAG_MILVUS_URI` |

## 2. 开发环境

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

- 首次启动自动下载嵌入模型（~100MB，默认走 `hf-mirror` 镜像；如可直连 huggingface.co 则设 `RAG_HF_ENDPOINT=` 置空）
- `data/manuals/` 下的说明书自动导入；删除 `data/milvus_lite.db` 可重建知识库
- 多 worker 注意：milvus-lite 为进程独占库，**单进程运行**；需要多 worker 请切换服务端 Milvus

## 3. 生产部署（docker compose）

```bash
cp .env.example .env          # 按需修改 LLM 等
docker compose up -d          # etcd + minio + milvus + api
```

- API 服务连接 `http://milvus:19530`，知识库目录 `./data/manuals` 挂载进容器，更新说明书后调用 `POST /api/v1/documents/scan` 热更新
- Milvus 镜像版本可通过修改 `docker-compose.yml` 中的 `image: milvusdb/milvus:v2.5.4` 升级
- 服务更新：`docker compose build api && docker compose up -d api`

### 资源建议

| 组件 | 规格 |
| --- | --- |
| API | 2C4G 起（嵌入模型常驻内存约 600MB；高并发建议 4C8G + 多实例） |
| Milvus Standalone | 4C8G 起，数据盘 SSD |
| 知识库规模 | 说明书 1 万份 / 10 万块级别下 HNSW 检索仍保持毫秒级 |

## 4. 配置项清单（前缀 `RAG_`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RAG_LLM_PROVIDER` | `mock` | `mock` 离线抽取式；`openai` 兼容 API |
| `RAG_LLM_BASE_URL` | 智谱 | OpenAI 兼容基地址 |
| `RAG_LLM_API_KEY` / `RAG_LLM_MODEL` | 空 / glm-4-flash | 凭证与模型 |
| `RAG_LLM_TEMPERATURE` | 0.1 | 越低越保守（防幻觉） |
| `RAG_EMBEDDING_PROVIDER` | `fastembed` | `fastembed` / `openai` |
| `RAG_FASTEMBED_MODEL` | bge-small-zh-v1.5 | 本地嵌入模型 |
| `RAG_MILVUS_URI` | `./data/milvus_lite.db` | 嵌入库路径或服务端 URL |
| `RAG_MILVUS_COLLECTION` | `manual_chunks` | 集合名 |
| `RAG_RETRIEVAL_TOP_K` | 6 | 召回条数 |
| `RAG_RETRIEVAL_SCORE_THRESHOLD` | 0.42 | 向量相关性阈值 |
| `RAG_KEYWORD_OVERLAP_MIN` | 0.10 | 关键词覆盖率下限 |
| `RAG_MAX_CONTEXT_CHUNKS` | 4 | 进入生成的最大块数 |
| `RAG_GENERATION_MAX_RETRIES` | 1 | 校验失败重试次数 |
| `RAG_HISTORY_WINDOW_MESSAGES` | 8 | 多轮历史窗口 |
| `RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP` | 480 / 64 | 分块参数（改动后需重新入库） |
| `RAG_MANUALS_DIR` / `RAG_UPLOADS_DIR` | ./data/... | 知识目录 |
| `RAG_AUTO_INGEST_ON_STARTUP` | true | 启动时自动导入 |
| `RAG_HOST` / `RAG_PORT` | 0.0.0.0 / 8000 | 监听地址 |
| `RAG_HYBRID_BM25_K` | 12 | 混合检索 BM25 路召回条数 |
| `RAG_RRF_K` | 60 | RRF 融合常量 |
| `RAG_RERANK_ENABLED` | false | 是否启用 bge-reranker 精排（依赖未安装时自动降级） |
| `RAG_AUTH_MODE` | `off` | `off` 演示（默认）/ `on` 生产（JWT + RBAC + 租户隔离） |
| `RAG_JWT_SECRET` | 空 | auth on 时必填，JWT 签名密钥 |
| `RAG_JWT_EXPIRE_HOURS` | 24 | Token 有效期（小时） |
| `RAG_DEFAULT_TENANT` | `default` | auth off 时的隐式租户名 |
| `RAG_TRACE_DB_PATH` | ./data/trace.db | 链路 span 持久化（可选） |
| `RAG_TRACE_TTL_SECONDS` | 3600 | 内存 trace 保留时长 |
| `RAG_ENDPOINT_METRICS_TTL` | 300 | 接口性能统计滑动窗口（秒） |
| `RAG_AUDIT_DB_PATH` | ./data/audit.db | 敏感操作审计日志 |
| `RAG_LLM_USAGE_DB_PATH` | ./data/llm_usage.db | LLM token 消耗统计 |
| `RAG_PROMPT_STORE_PATH` | ./data/prompts.json | 可热更新的 System Prompt |
| `RAG_RESOURCE_DB_PATH` | ./data/resource.db | 资源水位采样落库（CPU/内存/磁盘） |
| `RAG_RESOURCE_SAMPLE_INTERVAL` | 60 | 资源采样聚合间隔（秒） |
| `RAG_SLOW_REQUEST_THRESHOLD_MS` | 500 | 慢请求明细阈值（全程毫秒，超过即记录） |
| `RAG_NOTIFY_DB_PATH` | ./data/notify.db | 通知中心 SQLite 路径（用户通知，90 天自动清理） |
| `RAG_NOTIFY_RETENTION_DAYS` | 90 | 通知自动保留天数，超期自动清理（可配置） |
| `RAG_TICKET_DB_PATH` | ./data/tickets.db | 工单系统 SQLite 路径（工单 + 管理员回复） |

> **运行时动态配置层**：模型切换（LLM/Embedding）与检索参数（Top-K/阈值/RRF/Rerank）支持在监控中心 `/admin.html` 热更新，保存即生效、无需重启；未覆盖时恒等于上表的静态默认值，行为向后兼容。

## 5. 监控与可用性

- **健康检查**：`GET /api/v1/health`（milvus/embedding/llm 组件级状态），可接入负载均衡探活与 K8s liveness/readiness
- **性能指标**：每次回答返回 `metrics`（retrieval_ms/generation_ms/verify_ms/total_ms），建议日志采集后接入 Prometheus（`/chat` 调用方上报或中间件埋点）
- **可用性 > 99.9%**：
  - API 无状态，可水平扩容（会话在 SQLite，多实例需替换为 MySQL/PostgreSQL 或对接对象存储，接口已在 `ConversationStore` 单点封装）
  - Milvus Standalone 建议数据盘快照备份；`data/milvus_lite.db` 直接文件备份
  - LLM 不可用时自动降级路径：审核失败回退包含式校验；可在前端提示
- **知识库备份**：备份 `data/manuals`（源文档）即可，向量库可随时通过 `scripts/ingest.py` 重建

## 6. 知识库运维

```bash
# 导入/更新指定目录全部说明书
python -m scripts.ingest /path/to/manuals

# 或通过 API 热更新（不重启）
curl -X POST http://localhost:8000/api/v1/documents/scan -H 'Content-Type: application/json' -d '{}'
```

更新策略：文档以 `doc_name+size+mtime` 指纹识别，重复导入自动覆盖旧版本向量。

## 7. 安全建议

- 生产环境为 API 配置反向代理（nginx/traefik），启用 HTTPS 与访问控制
- `.env` 中的 API Key 不要提交版本库（已列入 `.gitignore`）
- Milvus 服务端启用认证：`RAG_MILVUS_USER` / `RAG_MILVUS_PASSWORD`
- 如需多租户，可在 Milvus filter 表达式中按 `doc_id`/租户字段隔离（接口已预留 `filter_expr` 参数）
