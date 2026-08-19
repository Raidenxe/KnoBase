"""评估脚本: 读取 eval/dataset.jsonl, 逐条检索并计算 Hits/Precision/Recall/MRR@k。

用法:
    python scripts/eval.py                 # 默认数据集 RAG_EVAL_DATASET_PATH
    python scripts/eval.py --top-k 1,3,5
    python scripts/eval.py --no-retrieve   # 仅加载数据集并校验格式(CI/冒烟)

输出: 控制台表格 + eval/report_<时间戳>.json(供 GET /api/v1/eval/summary 读取)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.core.embeddings import get_embedding_provider
from app.core.milvus_store import get_milvus_store
from app.core.retrieval import hybrid_retrieve


def load_dataset(path: str):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _retrieve(question: str, top_k: int, tenant_id: str):
    settings = get_settings()
    embedder = get_embedding_provider()
    vector = embedder.embed_query(question)
    result = hybrid_retrieve(
        get_milvus_store(), embedder, question, vector,
        top_k, settings.retrieval_score_threshold, tenant_id,
    )
    # 新返回契约: (hits, timings); 评测只需按顺序返回 hits
    return result[0] if isinstance(result, tuple) else result


def render_table(scores: dict) -> str:
    us = scores["_used"]
    header = f"{'指标':<14}" + "  ".join(f"@{k}" for k in us)
    lines = [header, "-" * len(header)]
    for name in ("Hits", "Precision", "Recall", "Mrr"):
        cells = "  ".join(f"{scores[f'{name}@{k}']:.3f}" for k in us)
        lines.append(f"{name:<14}" + cells)
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> Dict | None:
    import asyncio as aio

    from app.eval.registry import aggregate_retrieval_metrics

    settings = get_settings()
    ds_path = Path(args.dataset)
    if not ds_path.is_file():
        print(f"[eval] 数据集不存在: {ds_path}", file=sys.stderr)
        return None
    dataset = load_dataset(str(ds_path))
    print(f"[eval] 加载 {len(dataset)} 条评估用例")
    if not dataset:
        return None
    if args.no_retrieve:
        print("[eval] 冒烟模式: 数据集格式正常, 跳过检索")
        return None

    ks = [int(k) for k in args.top_k.split(",")]
    top_k = max(ks)
    cases = []
    for row in dataset:
        result = await aio.to_thread(
            _retrieve, row["question"], top_k, settings.default_tenant
        )
        cases.append({"result": result, "gold": set(row.get("gold_doc_ids", []))})

    scores = aggregate_retrieval_metrics(cases, ks=ks)
    scores["_used"] = ks
    print(f"\n=== 检索评估结果 ===")
    print(render_table(scores))

    report_dir = ds_path.parent
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = {
        "timestamp": ts,
        "cases": len(cases),
        "metrics": {k: v for k, v in scores.items() if k != "_used"},
        "top_k": ks,
    }
    out = report_dir / f"report_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入: {out}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG 检索评估")
    parser.add_argument("--dataset", default=str(get_settings().eval_dataset_path))
    parser.add_argument("--top-k", default="1,3,5")
    parser.add_argument("--no-retrieve", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()