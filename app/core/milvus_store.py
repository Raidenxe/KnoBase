"""Milvus 向量库封装: 集合管理 / 批量写入 / 相似检索 / 文档级增删查。

支持两种部署形态(由 RAG_MILVUS_URI 决定):
    - milvus-lite 嵌入式: "./data/milvus_lite.db"   (开发/演示, 零依赖)
    - Milvus 服务端:      "http://localhost:19530"  (docker-compose, 生产)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

SCHEMA_FIELDS = [
    "chunk_id",
    "text",
    "doc_id",
    "doc_name",
    "section_path",
    "chunk_index",
    "page",
    "tenant_id",
    "doc_version",
    "created_at",
]

# 检索/过滤时按租户隔离使用的表达式片段
def tenant_expr(tenant_id: str | None) -> str:
    return f'tenant_id == "{tenant_id}"' if tenant_id else ""


class MilvusStore:
    def __init__(self) -> None:
        import time as _time

        from pymilvus import MilvusClient

        settings = get_settings()
        self.collection = settings.milvus_collection
        kwargs: Dict[str, Any] = {"uri": settings.milvus_uri}
        if settings.milvus_user:
            kwargs["token"] = f"{settings.milvus_user}:{settings.milvus_password}"
        # milvus-lite 为文件锁; 上一个进程未完全退出时短暂持锁, 重试等待
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                self.client = MilvusClient(**kwargs)
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                _time.sleep(1.5 * (attempt + 1))
        if last_exc is not None:
            raise last_exc
        if self.client.has_collection(self.collection):
            try:
                self.client.load_collection(self.collection)
            except Exception as exc:  # noqa: BLE001
                logger.warning("加载集合 %s 失败(可能尚未就绪): %s", self.collection, exc)
        logger.info("Milvus 已连接: %s (collection=%s)", settings.milvus_uri, self.collection)

    # ------------------------------------------------------------------
    # 集合初始化
    # ------------------------------------------------------------------
    def ensure_collection(self, dim: int) -> None:
        if self.client.has_collection(self.collection):
            self.client.load_collection(self.collection)
            return
        from pymilvus import DataType

        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
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

        index_params = self.client.prepare_index_params()
        try:
            index_params.add_index(
                field_name="vector",
                index_type="HNSW",
                metric_type="COSINE",
                params={"M": 16, "efConstruction": 200},
            )
            self.client.create_collection(self.collection, schema=schema, index_params=index_params)
            logger.info("创建 Milvus 集合 %s (HNSW/COSINE, dim=%d)", self.collection, dim)
        except Exception as exc:  # noqa: BLE001
            logger.warning("HNSW 不可用(%s), 降级 FLAT 索引", exc)
            index_params = self.client.prepare_index_params()
            index_params.add_index(
                field_name="vector", index_type="FLAT", metric_type="COSINE"
            )
            self.client.create_collection(self.collection, schema=schema, index_params=index_params)
            logger.info("创建 Milvus 集合 %s (FLAT/COSINE, dim=%d)", self.collection, dim)

    # ------------------------------------------------------------------
    # 写入 / 删除
    # ------------------------------------------------------------------
    def insert_chunks(self, rows: List[Dict[str, Any]], batch: int = 64) -> int:
        for i in range(0, len(rows), batch):
            self.client.insert(
                collection_name=self.collection, data=rows[i : i + batch]
            )
        return len(rows)

    def delete_doc(self, doc_id: str, tenant_id: str | None = None) -> None:
        expr = f'doc_id == "{doc_id}"'
        t = tenant_expr(tenant_id)
        if t:
            expr = f'{expr} and {t}'
        self.client.delete(collection_name=self.collection, filter=expr)
        logger.info("已删除文档全部向量: doc_id=%s tenant=%s", doc_id, tenant_id)

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def search(
        self,
        query_vector: List[float],
        top_k: int,
        filter_expr: Optional[str] = None,
        score_threshold: float = 0.0,
        tenant_id: Optional[str] = None,
        doc_version: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """返回 [{"chunk_id","score","text","doc_id","doc_name","section_path",
        "chunk_index","page","tenant_id","doc_version"}], COSINE 越高越相关, 已按阈值过滤。"""
        expr = filter_expr or ""
        t = tenant_expr(tenant_id)
        if t:
            expr = f"{expr} and {t}" if expr else t
        if doc_version is not None:
            v = f'doc_version == {int(doc_version)}'
            expr = f"{expr} and {v}" if expr else v
        res = self.client.search(
            collection_name=self.collection,
            data=[query_vector],
            limit=top_k,
            output_fields=SCHEMA_FIELDS,
            filter=expr,
        )
        hits = res[0] if res else []
        out: List[Dict[str, Any]] = []
        for h in hits:
            # 兼容 pymilvus 2.x/3.x: id/distance 与 entity 内主键两种形态
            entity = dict(h.get("entity") or {})
            score = float(h.get("distance", h.get("score", 0.0)) or 0.0)
            if score < score_threshold:
                continue
            row = {
                "chunk_id": h.get("id") or entity.get("chunk_id"),
                "score": round(score, 4),
            }
            row.update({k: entity.get(k) for k in SCHEMA_FIELDS if k != "chunk_id"})
            out.append(row)
        return out

    def all_chunks(
        self, output_fields: Optional[List[str]] = None, limit: int = 16384,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """全量读取所有 chunk(用于构建 BM25 索引 / 备份)。
        返回每条含 chunk_id/text/doc_id/doc_name/tenant_id等标量字段。"""
        qfields = output_fields or SCHEMA_FIELDS
        filter_ = tenant_expr(tenant_id) or "chunk_index >= 0"
        rows = self.client.query(
            collection_name=self.collection,
            filter=filter_,
            output_fields=qfields,
            limit=limit,
        )
        if len(rows) >= limit:
            logger.warning("all_chunks 达到 limit=%d 上限, 可能存在截断", limit)
        return rows

    # ------------------------------------------------------------------
    # 文档清单 / 统计
    # ------------------------------------------------------------------
    def list_documents(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self.client.query(
            collection_name=self.collection,
            filter=tenant_expr(tenant_id) or "chunk_index >= 0",
            output_fields=["doc_id", "doc_name", "doc_version", "created_at"],
            limit=16384,
        )
        agg: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            doc = agg.setdefault(
                r["doc_id"],
                {"doc_id": r["doc_id"], "doc_name": r["doc_name"], "chunks": 0,
                 "doc_version": r.get("doc_version"), "created_at": r.get("created_at", 0)},
            )
            doc["chunks"] += 1
            # Milvus 只保留激活版本, 若历史混入多版本则取最大版本号
            if r.get("doc_version") is not None:
                if doc.get("doc_version") is None or r["doc_version"] > doc["doc_version"]:
                    doc["doc_version"] = r["doc_version"]
        return sorted(agg.values(), key=lambda d: -d["created_at"])

    def count(self) -> int:
        stats = self.client.get_collection_stats(self.collection)
        return int(stats.get("row_count", 0))


_store: Optional[MilvusStore] = None


def get_milvus_store() -> MilvusStore:
    global _store
    if _store is None:
        _store = MilvusStore()
    return _store


def now_ms() -> int:
    return int(time.time() * 1000)
