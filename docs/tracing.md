# 全链路追踪与可观测性

## 1. 链路 ID

全局 `TracingMiddleware` 为每位请求生成 `trace_id`（可透传 `X-Request-Id`），并写回响应头。
对话图各节点（`analyze / retrieve / grade / generate / verify`）在 `app/graph/nodes.py` 内通过 `state["trace_id"]` 打点 span，由 `TraceStore` 记录各阶段耗时。

## 2. TraceStore

- 内存环形缓冲（最多保留约 2000 条，TTL 由 `RAG_TRACE_TTL_SECONDS` 控制）。
- 可选落库 `data/trace.db`（`RAG_TRACE_DB_PATH`）实现持久化，不阻塞关键路径。

## 3. 观测接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/v1/traces` | 最近请求链路列表 |
| `GET /api/v1/traces/{trace_id}` | 单次请求全流程阶段耗时 |
| `GET /api/v1/metrics/basic` | 分阶段 P50/P95/峰值、失败率汇总 |
| `GET /api/v1/eval/summary` | 最近一次评估报告摘要（仅 admin） |

聊天响应（同步/SSE/WebSocket 的 `done` 事件）均携带 `trace_id`，便于前后端对账。

## 4. 回答阶段的链路样本

```
trace_id=abc123
  analyze    → 12ms
  retrieve   → 38ms (召回 12 条)
  grade      → 45ms (通过 4 条)
  generate   → 320ms
  verify     → 60ms
```

## 5. 与评估联动

评估脚本执行检索后，可对照 `metrics/basic` 观察线上各阶段耗时与失败率变化。