# RAG 回答质量评估指标
## KnoBase RAG智能助手

| 元数据 | 内容 |
|---|---|
| 文档编号 | DOC-EVAL-001 |
| 密级 | 内部 |
| 版本 | v1.0 |
| 适用范围 | 研发、QA、算法调优 |
| 维护人 | 平台研发团队 |
| 上次更新 | 2026-08-19 |

**修订历史**

| 版本 | 日期 | 修订人 | 变更摘要 |
|---|---|---|---|
| v1.0 | 2026-08-19 | 平台研发 | 建立检索侧量化指标，并与 `tests/TEST_CASES.md` 的 Q 类用例对接 |

---

## 1. 目的与定位

本系统回答质量 = **检索侧召回质量 × 生成侧忠实度**。在当前未配置真实 LLM（Mock 模式）的环境下，
生成侧（Q-01/Q-03/Q-04）标记为「待真实模型联验」，**检索侧（Q-02）可离线量化精校**，
因此本版以检索侧指标作为参数精校的主通道，为其建模客观衡量标准。

指标计算为**纯函数**，位于 `app/eval/registry.py`，并可在 `scripts/eval.py` 与 `scripts/tune_params.py` 中复用。

## 2. 指标定义

设某问题的金标源文档集合为 `gold`，检索结果为有序列表 `result[:k]`（按相关度降序）：

| 指标 | 公式/含义 | 评价侧重 |
|---|---|---|
| `Hits@k` | 前 k 个结果是否含至少一个 `gold` 命中（0/1），对所有用例取均值 | 召回可达性 |
| `Precision@k` | 前 k 条中命中条数占比 | 精确度/噪声比 |
| `Recall@k` | 前 k 条命中的 `gold` 项数 / 全部 `gold` 项数 | 完整覆盖 |
| `MRR@k` | 首个命中所在名次的倒数均值 | 排序质量/首位置 |

聚合入口：`aggregate_retrieval_metrics(results, ks=(1,3,5))`。综合分（用于配置排序）：
`composite = 0.5×Hits@1 + 0.5×MRR@3`。

## 3. 与测试用例集的对接

| 测试用例 | 指标（自动化） | 目标阈值 | 评估集 |
|---|---|---|---|
| Q-02 | Hits@1/3/5、MRR@5 | Hits@1≥0.7、MRR@5≥0.8 | `eval/dataset_kb.jsonl`（见节 4） |

> 说明：原先 `eval/dataset.jsonl` 的金标 `doc_id`（`data_gateway`/`support_manual`）与当前知识库（`data/manuals/` 55 篇）
> 实际文档 id 不匹配，属无效评估集，**已用 `eval/dataset_kb.jsonl` 替代**（该集金标与真实文档 id 一一对应）。

## 4. 评估数据集

`eval/dataset_kb.jsonl`：30 条问题，覆盖 K8s / Docker / Linux / 网络 / MySQL / Redis / 部署 / 面试等主题，
每条 `gold_doc_ids` 指向知识库中的真实文档 id。可用于 `scripts/eval.py --dataset eval/dataset_kb.jsonl` 复跑。

## 5. 判定口径

- 命中判定以 `doc_id` 相等为准（同文档任一 chunk 命中即视为该文档命中）。
- 端到端回答质量（Q-01/Q-03/Q-04）需在配置真实 LLM 后，以金标答案进行人工/LLM 判定，并记录一致率、召回度、拒答率。