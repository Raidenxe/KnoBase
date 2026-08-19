"""知识库入库 CLI:

    python -m scripts.ingest [目录]     # 缺省使用 RAG_MANUALS_DIR
"""

import json
import sys

from app.config import get_settings
from app.knowledge.pipeline import run_ingest


def main() -> None:
    directory = sys.argv[1] if len(sys.argv) > 1 else get_settings().manuals_dir
    results = run_ingest(directory)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    total = sum(r.get("chunks", 0) for r in results if isinstance(r, dict))
    print(f"\n完成: {len(results)} 个文档 / {total} 个知识块 已写入 Milvus")


if __name__ == "__main__":
    main()
