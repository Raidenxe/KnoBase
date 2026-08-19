"""Milvus 集合 schema 迁移: 为旧集合补充 tenant_id / doc_version 字段。

背景: 旧集合按旧 schema 创建, 缺少 tenant_id / doc_version。Milvus 不支持
原地加列(Milvus 2.4 前), 因此采用「建新集合→批量迁移数据→原子切换集合名」:

    1. 创建 {collection}_v2(新 schema)
    2. 全量读取旧集合数据 → 写入 v2(补默认 tenant_id / doc_version)
    3. 删除旧集合 → 将 v2 重命名为正式集合名

仅当集合存在且缺失新字段时执行; 已是最新 schema 则提示并跳过。

用法:
    python scripts/migrate_schema.py            # 打印迁移计划并确认
    python scripts/migrate_schema.py --force    # 直接执行, 不询问
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.core.milvus_store import SCHEMA_FIELDS, MilvusStore


def _existing_fields(client, collection: str) -> set:
    from pymilvus import MilvusClient

    schema = client.describe_collection(collection)["schema"]
    return {f["name"] for f in schema["fields"]}


def migrate(force: bool = False) -> None:
    settings = get_settings()
    store = MilvusStore()
    client = store.client
    col = settings.milvus_collection
    if not client.has_collection(col):
        print(f"[migrate] 集合不存在: {col}, 将由 ensure_collection 全新创建, 无需迁移")
        return

    fields = _existing_fields(client, col)
    missing = [f for f in ("tenant_id", "doc_version") if f not in fields]
    if not missing:
        print(f"[migrate] 集合 {col} 已是新 schema(含 tenant_id/doc_version), 跳过")
        return

    print(f"[migrate] 集合 {col} 缺失字段: {missing}")
    print("[migrate] 将重建集合并迁移数据(全部 chunk 补默认字段)。")
    if not force:
        if input("确认重建并迁移? [y/N] ").strip().lower() != "y":
            print("[migrate] 已取消")
            return

    embedder = None  # 迁移不重向量化: 直接搬运既有向量
    v2 = f"{col}_v2_migrate"
    # 复用 ensure 逻辑创建 v2: 直接调内部建库流程
    store.collection = v2
    try:
        dim = _vector_dim(client, col)
        _create_v2(client, dim, v2)
        _copy_data(client, col, v2, store)
        client.drop_collection(col)
        client.rename_collection(v2, col)
        print(f"[migrate] 完成: 旧集合 {col} → 重建迁移成功, 数据已保留")
    finally:
        store.collection = col


def _vector_dim(client, col: str) -> int:
    schema = client.describe_collection(col)["schema"]
    for f in schema["fields"]:
        if f["params"]["field_data_type"] == "FLOAT_VECTOR" or (
            f["type"] == 101
        ):
            return int(f["params"]["dim"])
    raise RuntimeError(f"无法解析 {col} 向量维度")


def _create_v2(client, dim: int, name: str) -> None:
    from pymilvus import DataType

    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
    schema.add_field("text", DataType.VARCHAR, max_length=8192)
    schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
    schema.add_field("doc_name", DataType.VARCHAR, max_length=512)
    schema.add_field("section_path", DataType.VARCHAR, max_length=1024)
    schema.add_field("chunk_index", DataType.INT64)
    schema.add_field("page", DataType.INT64)
    schema.add_field("tenant_id", DataType.VARCHAR, max_length=64)
    schema.add_field("doc_version", DataType.INT64)
    schema.add_field("created_at", DataType.INT64)
    idx = client.prepare_index_params()
    idx.add_index(field_name="vector", index_type="HNSW", metric_type="COSINE",
                  params={"M": 16, "efConstruction": 200})
    client.create_collection(name, schema=schema, index_params=idx)


def _copy_data(client, src: str, dst: str, store) -> None:
    limit = 2048
    offset = 0
    moved = 0
    while True:
        rows = client.query(collection_name=src, filter="chunk_index >= 0",
                            output_fields=SCHEMA_FIELDS, limit=limit, offset=offset)
        if not rows:
            break
        for r in rows:
            r.setdefault("tenant_id", get_settings().default_tenant)
            r.setdefault("doc_version", 1)
            r.pop("id", None)
        client.insert(collection_name=dst, data=rows)
        moved += len(rows)
        offset += len(rows)
        if len(rows) < limit:
            break
    print(f"[migrate] 已迁移 {moved} 条 chunk")


def main() -> None:
    parser = argparse.ArgumentParser(description="Milvus 集合 schema 迁移")
    parser.add_argument("--force", action="store_true", help="不询问直接执行")
    args = parser.parse_args()
    migrate(force=args.force)


if __name__ == "__main__":
    main()