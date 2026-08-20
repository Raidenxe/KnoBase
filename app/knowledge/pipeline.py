"""知识入库流水线: 加载 → 分块 → 向量化 → Milvus 写入(幂等, 支持更新)。"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List

from app.config import get_settings
from app.core.bm25 import upsert_chunk
from app.core.embeddings import get_embedding_provider
from app.core.milvus_store import get_milvus_store
from app.core.runtime_config import get_runtime_config
from app.knowledge.loader import SUPPORTED_EXTS, load_file
from app.knowledge.splitter import split_blocks
from app.services.doc_meta import get_doc_meta_store
from app.services.versions import get_version_store

logger = logging.getLogger(__name__)


class IngestPipeline:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.embedder = get_embedding_provider()
        self.store = get_milvus_store()
        self.store.ensure_collection(self.embedder.dimension)

    # ------------------------------------------------------------------
    @staticmethod
    def compute_doc_id(path: Path, doc_name: str) -> str:
        return hashlib.sha1(
            f"{doc_name}::{path.stat().st_size}::{path.stat().st_mtime}".encode()
        ).hexdigest()[:16]

    def ingest_file(
        self,
        path: str | Path,
        doc_name: str | None = None,
        skip_existing: bool = False,
        tenant_id: str | None = None,
        created_by: str = "",
        doc_id: str | None = None,
    ) -> Dict[str, Any]:
        """导入单个文档。

        skip_existing=True 时, 若同一指纹(doc_name+大小+mtime)的文档已入库,
        直接跳过(增量模式, 避免重复向量化)。
        tenant_id: 所属租户(多租户隔离); created_by: 操作人(版本记录)。
        doc_id: 可选, 强制指定目标 doc_id(覆盖上传/回滚需保持文档身份不变时使用)。
        """
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"文件不存在: {path}")
        tenant_id = tenant_id or get_settings().default_tenant
        doc_name = doc_name or path.stem
        doc_id = doc_id or self.compute_doc_id(path, doc_name)
        if skip_existing and doc_id in {d["doc_id"] for d in self.store.list_documents(tenant_id)}:
            return {
                "doc_id": doc_id,
                "doc_name": doc_name,
                "chunks": 0,
                "duration_ms": 0,
                "skipped": True,
            }

        t0 = time.perf_counter()
        loaded = load_file(path)
        # 切片参数支持运行时覆盖(仅对新导入/覆盖的文档生效)
        _rt = get_runtime_config()
        _chunk_size = _rt.get("chunk_size", self.settings.chunk_size)
        _chunk_overlap = _rt.get("chunk_overlap", self.settings.chunk_overlap)
        chunks = split_blocks(
            loaded.blocks, doc_name, _chunk_size, _chunk_overlap
        )
        if not chunks:
            return {"doc_id": doc_id, "doc_name": doc_name, "chunks": 0, "duration_ms": 0}

        fingerprint = f"{path.stat().st_size}:{path.stat().st_mtime}"
        version = get_version_store().save_version(
            doc_id, doc_name, fingerprint, len(chunks), str(path),
            [{"chunk_index": c.chunk_index, "section_path": c.section_path,
              "page": c.page, "text": c.text} for c in chunks],
            tenant_id, created_by,
        )

        vectors = self.embedder.embed_documents([c.text for c in chunks])
        now = int(time.time())
        rows = []
        for c, vec in zip(chunks, vectors):
            rows.append(
                {
                    "chunk_id": f"{doc_id}_{c.chunk_index:05d}",
                    "vector": vec,
                    "text": c.text[:8000],
                    "doc_id": doc_id,
                    "doc_name": doc_name[:500],
                    "section_path": (c.section_path or "正文")[:1000],
                    "chunk_index": c.chunk_index,
                    "page": c.page if c.page >= 0 else -1,
                    "tenant_id": tenant_id,
                    "doc_version": version,
                    "created_at": now,
                }
            )
        self.store.delete_doc(doc_id, tenant_id)  # 幂等: 同名/同版本文档先删后插(更新)
        self.store.insert_chunks(rows)
        # 同步维护 BM25 关键词索引(幂等替换旧 chunk)
        for r in rows:
            upsert_chunk(r["chunk_id"], r["text"], tenant_id,
                         {k: r[k] for k in ("doc_id", "doc_name", "section_path",
                                            "chunk_index", "page", "tenant_id", "doc_version", "text")})
        duration = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "入库完成: %s → %d 块 / 版本 %d / %d 向量 (%d ms)",
            doc_name, len(chunks), version, len(rows), duration
        )
        return {
            "doc_id": doc_id,
            "doc_name": doc_name,
            "chunks": len(chunks),
            "version": version,
            "duration_ms": duration,
        }

    # ------------------------------------------------------------------
    def ingest_paths(
        self, paths: Iterable[str | Path], skip_existing: bool = False,
        tenant_id: str | None = None, created_by: str = "",
    ) -> List[Dict[str, Any]]:
        results, errors = [], []
        for p in paths:
            try:
                results.append(
                    self.ingest_file(p, skip_existing=skip_existing,
                                     tenant_id=tenant_id, created_by=created_by)
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"path": str(p), "error": str(exc)})
                logger.error("入库失败 %s: %s", p, exc)
        return {"imported": results, "errors": errors} if errors else results

    def ingest_directory(
        self,
        directory: str | Path,
        incremental: bool = False,
        progress=None,
        tenant_id: str | None = None,
        created_by: str = "",
    ) -> List[Dict[str, Any]]:
        """扫描目录批量导入。

        incremental=True: 指纹未变化的文档跳过(启动自动导入/重复扫描场景)
        progress: 可选回调 progress(doc_name, done, total, result), 用于后台任务上报
        """
        directory = Path(directory)
        if not directory.is_dir():
            raise FileNotFoundError(f"目录不存在: {directory}")
        paths = sorted(
            p for p in directory.rglob("*") if p.suffix.lower() in SUPPORTED_EXTS
        )
        if not paths:
            return []
        logger.info("扫描目录 %s: 发现 %d 个文档", directory, len(paths))

        # 增量模式: 预取已入库指纹, 一次查询过滤全部未变化文档
        todo = paths
        if incremental:
            existing = {d["doc_id"] for d in self.store.list_documents(tenant_id)}
            todo = [
                p for p in paths
                if self.compute_doc_id(p, p.stem) not in existing
            ]
            skipped = len(paths) - len(todo)
            if skipped:
                logger.info("增量模式: 跳过 %d 个未变化文档, 待处理 %d 个", skipped, len(todo))
            if not todo:
                return []

        results = []
        for i, p in enumerate(paths, start=1):
            if p not in todo:
                continue
            try:
                r = self.ingest_file(p, tenant_id=tenant_id, created_by=created_by)
                results.append(r)
                if progress:
                    progress(p.stem, i, len(paths), r)
                # 根据子目录结构设置文档分类和访问权限
                if "doc_id" in r and r.get("chunks", 0) > 0:
                    self._apply_doc_meta(p, directory, r["doc_id"], tenant_id, created_by)
            except Exception as exc:  # noqa: BLE001
                logger.error("入库失败 %s: %s", p, exc)
                results.append({"path": str(p), "error": str(exc)})
                if progress:
                    progress(p.stem, i, len(paths), {"error": str(exc)})
        return results

    def _apply_doc_meta(
        self, file_path: Path, base_dir: Path, doc_id: str,
        tenant_id: str | None = None, created_by: str = "",
    ) -> None:
        """根据文件所在子目录设置文档分类和访问权限。

        规则:
            - 根目录下的文件不设分类(空)
            - 第一级子目录名作为分类名
            - 分类名为"平台API"时自动设为 private(仅管理员可见)
        """
        try:
            rel = file_path.parent.relative_to(base_dir)
            if rel == Path("."):
                return  # 根目录文件不设分类
            category = rel.parts[0]  # 第一级子目录作为分类名
            access_scope = "private" if category == "平台API" else None
            get_doc_meta_store().set_meta(
                doc_id, tenant_id or get_settings().default_tenant,
                category=category, access_scope=access_scope,
                by=created_by or "system",
            )
        except Exception:  # noqa: BLE001
            logger.exception("设置文档分类/权限失败: %s", doc_id)


def run_ingest(directory: str | Path | None = None) -> List[Dict[str, Any]]:
    """便捷入口: 默认导入配置的说明书目录"""
    settings = get_settings()
    return IngestPipeline().ingest_directory(directory or settings.manuals_dir)
