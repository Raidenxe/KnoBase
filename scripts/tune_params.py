"""检索参数系统性精校脚本: 对 eval/dataset_kb.jsonl 逐条检索并计算 Hit/Precision/Recall/MRR@k。

采用「分轴敏感性与组合寻优」两阶段:
    - ROUND 1(分轴敏感性): 围绕基线逐变量扫描, 找每个参数的最优取向
    - ROUND 2(组合寻优): 取各轴 Top 候选做小规模交叉, 收敛到全局更优组合

用法(需在停服状态下运行, 以独占 milvus-lite 锁):
    .venv/bin/python scripts/tune_params.py --round 1
    .venv/bin/python scripts/tune_params.py --round 2
    .venv/bin/python scripts/tune_params.py --all      # 依序跑 1+2

输出:
    - 控制台对比表
    - eval/report_tune_<round>.json(全量逐配置指标)
    - eval/param_tuning_report.md(合并后的精校报告, 供交付)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.core.embeddings import get_embedding_provider  # noqa: E402
from app.core.milvus_store import get_milvus_store  # noqa: E402
from app.core.retrieval import hybrid_retrieve  # noqa: E402
from app.core.runtime_config import get_runtime_config  # noqa: E402
from app.core.settings_rt import effective_settings  # noqa: E402
from app.eval.registry import aggregate_retrieval_metrics  # noqa: E402

DATASET = Path(__file__).resolve().parent.parent / "eval" / "dataset_kb.jsonl"
EVAL_DIR = DATASET.parent

KS = [1, 3, 5]
METRIC_ORDER = ("Hits", "Precision", "Recall", "MRR")

# 基线(与 config.py 静_默认一致): top_k=12, threshold=0.42, bm25_k=12, rrf_k=60, 权重 1:1
BASELINE = {
    "retrieval_top_k": 12,
    "retrieval_score_threshold": 0.42,
    "hybrid_bm25_k": 12,
    "rrf_k": 60,
    "rrf_vector_weight": 1.0,
    "rrf_bm25_weight": 1.0,
}

# 分轴扫描轴(ROUND 1): 每项是 (字段, 候选值集), None=保持基线
AXES = {
    "retrieval_top_k": [5, 8, 12, 16],
    "retrieval_score_threshold": [0.0, 0.30, 0.42, 0.55],
    "hybrid_bm25_k": [8, 12, 20],
    "rrf_k": [30, 60, 100],
    "rrf_bm25_weight": [0.5, 1.0, 2.0],  # 与 rrf_vector_weight=1.0 固定对照
}


def load_dataset(path: Path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def composite(metrics: dict) -> float:
    """综合分: 首击质量 + 平均名次 的均衡化评价(用于排序/选取)。"""
    return round(0.5 * metrics.get("Hits@1", 0.0) + 0.5 * metrics.get("MRR@3", 0.0), 4)


def _retrieve_one(query, qvec, top_k, threshold, tenant_id):
    store = get_milvus_store()
    embedder = get_embedding_provider()
    result = hybrid_retrieve(
        store, embedder, query, qvec, top_k, threshold, tenant_id
    )
    return result[0] if isinstance(result, tuple) else result


def run_config(cases, qvecs, cfg: dict, tenant_id: str) -> dict:
    rt = get_runtime_config()
    rt.clear()
    ordered = None
    for field, value in cfg.items():
        if value is None:
            rt.reset(field)
        else:
            rt.set(field, value)
            if field == "retrieval_top_k":
                ordered = value
    eff = effective_settings()
    top_k = ordered if ordered is not None else eff.retrieval_top_k
    threshold = eff.retrieval_score_threshold
    results = []
    for (row, qvec) in zip(cases, qvecs):
        results.append({"result": _retrieve_one(row["question"], qvec, top_k, threshold, tenant_id),
                        "gold": set(row.get("gold_doc_ids", []))})
    m = aggregate_retrieval_metrics(results, ks=KS)
    m = {("MRR@" + k.split("@", 1)[1] if k.startswith("Mrr") else k): v for k, v in m.items()}
    m["_used"] = KS
    return m


def build_round1_configs() -> list[dict]:
    confs = [dict(BASELINE)]  # 第 0 个为基线
    for field, values in AXES.items():
        for v in values:
            c = dict(BASELINE)
            c[field] = v
            confs.append(c)
    return confs


def build_round2_configs(round1_best: dict, cfg: dict) -> list[dict]:
    """以 ROUND1 的有利方向做定向小网格寻优(12 组合)。"""
    candidates = [dict(cfg)]  # 基线对照
    for tk in (8, 12):                      # ROUND1: top_k 对指标不敏感 → 取小值降时延
        for bk in (6, 8, 10):               # ROUND1: hybrid_bm25_k=8 最优, 邻域细化
            for w in (1.5, 2.0):            # ROUND1: 上调 BM25 权重有益
                c = dict(cfg)
                c.update({"retrieval_top_k": tk, "hybrid_bm25_k": bk, "rrf_bm25_weight": w})
                candidates.append(c)
    return list({json.dumps(c, sort_keys=True): c for c in candidates}.values())


def build_round3_configs(round2_best: dict, cfg: dict) -> list[dict]:
    """围绕 ROUND2 最优做细粒度精调(3×3=9 组合), 验证是否收敛于平台期。"""
    candidates = [dict(cfg)]
    for w in (1.25, 1.5, 1.75):
        for bk in (7, 8, 9):
            c = dict(round2_best)
            c.update({"hybrid_bm25_k": bk, "rrf_bm25_weight": w, "retrieval_top_k": 8})
            candidates.append(c)
    return list({json.dumps(c, sort_keys=True): c for c in candidates}.values())


def render_table(results: list[tuple[dict, dict]]) -> str:
    header = f"{'配置(前3轴)':<22}" + "  ".join(f"{m}@{k}" for k in KS for m in ("H", "P", "R", "M"))
    lines = [f"{'方案':<6}{header}", "-" * len(f"{'方案':<6}{header}")]
    for i, (cfg, metrics) in enumerate(results):
        tag = f"[{i}]"
        brief = ",".join(f"{k}={v}" for k, v in cfg.items() if v is not None)
        cells = "  ".join(
            f"{metrics[f'{m}@{k}']:.3f}" for k in KS for m in ("Hits", "Precision", "Recall", "MRR")
        )
        lines.append(f"{tag:<6}{brief:<22}{cells}")
    lines.append(f"{'[基线]':<6}{'top_k=12,thr=0.42,bm=12,rrf=60,w=1:1':<22}")
    return "\n".join(lines)


def run_round(round_no: int, args) -> None:
    ds_path = Path(args.dataset)
    cases = load_dataset(ds_path)
    print(f"[round{round_no}] 加载 {len(cases)} 条评估用例")
    tenant_id = get_settings().default_tenant

    embedder = get_embedding_provider()
    qvecs = [embedder.embed_query(row["question"]) for row in cases]

    if round_no == 1:
        configs = build_round1_configs()
    else:
        prev = EVAL_DIR / f"report_tune_{round_no - 1}.json"
        if not prev.is_file():
            print(f"[round{round_no}] 需先运行 round{round_no - 1} 生成 {prev.name}", file=sys.stderr)
            return
        pbest = json.loads(prev.read_text(encoding="utf-8"))["best"]
        if round_no == 2:
            configs = build_round2_configs(pbest, BASELINE)
        else:
            configs = build_round3_configs(pbest, BASELINE)

    results = []
    for cfg in configs:
        t0 = time.perf_counter()
        m = run_config(cases, qvecs, cfg, tenant_id)
        m = {k: v for k, v in m.items() if k != "_used"}
        m["_composite"] = composite(m)
        m["_seconds"] = round(time.perf_counter() - t0, 2)
        results.append((cfg, m))
        print(f"  cfg {json.dumps(cfg)} score={m['_composite']:.4f} "
              f"H@1={m['Hits@1']:.3f} MRR@5={m['MRR@5']:.3f} ({m['_seconds']}s)")

    # 基线单独展示
    base_metrics = dict(dict(BASELINE))
    for _, m in results:
        if m.get("retrieval_top_k") is not None and m["retrieval_top_k"] == 12 \
                and m.get("retrieval_score_threshold", 0.42) == 0.42:
            base_metrics = m
            break

    results_sorted = sorted(results, key=lambda x: -x[1]["_composite"])
    print("\n" + render_table(results_sorted[: len(results_sorted)]))

    best_cfg, best_m = results_sorted[0]
    print(f"\n[round{round_no}] 最优配置: {json.dumps(best_cfg, ensure_ascii=False)}")
    print({k: v for k, v in best_m.items() if not k.startswith("_")})

    out = EVAL_DIR / f"report_tune_{round_no}.json"
    out.write_text(json.dumps({
        "round": round_no,
        "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "dataset": str(ds_path.name),
        "cases": len(cases),
        "configs": [{"config": c, "metrics": m} for c, m in results],
        "baseline": base_metrics,
        "best": {k: v for k, v in best_cfg.items()},
        "best_metrics": {k: v for k, v in best_m.items() if not k.startswith("_")},
        "composite": best_m["_composite"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="检索参数系统性精校")
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--round", type=int, choices=[1, 2, 3], default=1)
    parser.add_argument("--all", action="store_true", help="依序运行 round1+round2")
    args = parser.parse_args()

    if args.all:
        run_round(1, args)
        run_round(2, args)
    else:
        run_round(args.round, args)


if __name__ == "__main__":
    main()