"""产品说明书管理接口: 批量导入/上传/目录扫描/网页导入/删除/清单

耗时导入操作支持后台异步执行(background=true), 立即返回 task_id,
通过 GET /api/v1/tasks/{task_id} 查询进度, 不阻塞正常问答服务。
"""

from __future__ import annotations

import csv
import io
import os
import time
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app.config import get_settings
from app.core.milvus_store import get_milvus_store, tenant_expr
from app.core.security import (
    auth_enabled,
    current_tenant_id,
    read_required,
    write_required,
)
from app.knowledge.loader import SUPPORTED_EXTS
from app.knowledge.pipeline import IngestPipeline
from app.knowledge.web_import import save_webpage
from app.services.doc_meta import get_doc_meta_store
from app.services.doc_records import (
    SUPPORTED_UPLOAD_EXTS,
    format_of,
    get_doc_record_store,
)
from app.services.tasks import get_task_manager
from app.services.audit import audit
from app.services.versions import get_version_store

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


class ImportRequest(BaseModel):
    paths: List[str]


class ScanRequest(BaseModel):
    directory: Optional[str] = None


class BatchDeleteRequest(BaseModel):
    doc_ids: List[str]


class DocEditRequest(BaseModel):
    text: str  # 编辑后的文档全文(以 markdown/纯文本组织), 保存时重新切片+向量化


class WebImportRequest(BaseModel):
    urls: List[str]
    background: bool = False


class DocMetaUpdate(BaseModel):
    category: Optional[str] = None
    access_scope: Optional[str] = None


class BatchMetaRequest(BaseModel):
    doc_ids: List[str]
    category: Optional[str] = None
    access_scope: Optional[str] = None


def _pipeline() -> IngestPipeline:
    return IngestPipeline()


def _enrich_meta(docs: List[dict], tenant_id: str, user: dict) -> List[dict]:
    """为文档清单附加 category / access_scope, 并按可见权限过滤。

    权限规则(知识库浏览/管理的可见性控制, 检索仍以租户隔离):
        - admin/owner 或授权关闭: 可见全部(含 private);
        - 非 admin: 仅可见本租户内 access_scope != private 且 分类∈可读范围(或未分类) 的文档。
    """
    if not docs:
        return []
    # 按租户取批量元数据(单次 SQL)再补全
    meta = get_doc_meta_store().get_many_meta([d["doc_id"] for d in docs], tenant_id)
    is_admin = user.get("role") == "admin"
    from app.core.access import category_scope, hits_category_scope

    readable, _ = category_scope(user)  # None=不限; 否则为可读分类名列表
    scope_filter = hits_category_scope(readable, {}) if readable is not None else None
    out: List[dict] = []
    for d in docs:
        m = meta.get(d["doc_id"], {})
        row = dict(d)
        cat = m.get("category", "") or ""
        row["category"] = cat
        row["access_scope"] = m.get("access_scope", "tenant")
        row["viewable"] = is_admin or row["access_scope"] != "private"
        # 分类授权过滤
        if scope_filter is not None and not is_admin:
            if cat and cat not in scope_filter:
                row["viewable"] = False
        if row["viewable"]:
            out.append(row)
    return out


def _ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _doc_viewable(meta: dict, user: dict) -> bool:
    """单篇文档是否对当前用户可见(与 _enrich_meta 的清单过滤保持一致)。

    - 演示模式(auth off)/匿名: 一律可见;
    - admin: 可见全部(含 private);
    - 其他: private 不可见; 需 未分类 或 分类∈可读范围。
    """
    if not auth_enabled() or user.get("is_anonymous"):
        return True
    if user.get("role") == "admin":
        return True
    if meta.get("access_scope") == "private":
        return False
    cat = meta.get("category", "") or ""
    from app.core.access import readable_categories

    readable = readable_categories(user)  # None=不限(owner); 否则为可读分类列表
    if readable is None:
        return True
    return (not cat) or cat in set(readable)


def _raise_unless_doc_viewable(doc_id: str, tenant_id: str, user: dict, meta: dict) -> None:
    """读取接口前置校验: 无权限则 403。"""
    if not _doc_viewable(meta, user):
        raise HTTPException(403, "无权访问该文档(分类/可见范围不允许)")


def _doc_meta(doc_id: str, tenant_id: str) -> dict:
    return get_doc_meta_store().get(doc_id, tenant_id) or {}


def _notify_kb_update(tenant_id: str, detail: str) -> None:
    """知识库更新公告 → 通知中心(广播本租户全部用户)。认证关闭时不落库。"""
    from app.services.notify_store import T_KB_UPDATE, get_notify_store, tenant_user_ids

    uids = tenant_user_ids(tenant_id)
    if not uids:
        return
    get_notify_store().make_broadcast(
        tenant_id, uids, T_KB_UPDATE, "知识库更新",
        f"管理员已上传/更新文档，可重新搜索命中。<br>{detail}",
        link="/documents-browser",
    )


def _run_scan_task(task: dict) -> dict:
    """后台线程中执行目录扫描(带进度上报)"""
    directory = task["params"]["directory"]
    incremental = task["params"]["incremental"]
    tenant_id = task["params"].get("tenant_id")
    created_by = task["params"].get("created_by", "")
    pipeline = _pipeline()

    def progress(doc_name: str, done: int, total: int, result: dict) -> None:
        get_task_manager().update(task["id"], done=done, total=total, current=doc_name)

    results = pipeline.ingest_directory(
        directory, incremental=incremental, progress=progress,
        tenant_id=tenant_id, created_by=created_by,
    )
    return {
        "directory": directory,
        "imported": [r for r in results if isinstance(r, dict) and "error" not in r],
        "errors": [r for r in results if isinstance(r, dict) and "error" in r],
        "imported_count": len([r for r in results if isinstance(r, dict) and "error" not in r]),
    }


@router.get("", summary="已导入文档清单", dependencies=[Depends(read_required)])
def list_documents(
    category: Optional[str] = None,
    access_scope: Optional[str] = None,
    search: Optional[str] = None,
    status: Optional[str] = None,
    page: Optional[int] = None,
    per_page: Optional[int] = 20,
    user: dict = Depends(read_required),
) -> dict:
    """文档清单(管理面板)。

    结合两个事实源:
        - Milvus 已向量化的成功文档(权威);
        - 管理面板记录表: 补充 processing/failed 记录与展示名/版本/格式/大小。
    搜索按展示名/原文件名模糊; 默认按上传时间倒序; page/per_page 可空(空则返回全部)。
    """
    tenant_id = current_tenant_id(user)
    milvus_docs = get_milvus_store().list_documents(tenant_id)
    recs = get_doc_record_store().list_all(tenant_id)
    rec_by_id = {r["doc_id"]: r for r in recs}

    combined: dict = {}
    for d in milvus_docs:
        rec = rec_by_id.get(d["doc_id"])
        if rec:
            name = rec["display_name"] or d["doc_name"]
            combined[d["doc_id"]] = {
                **d, "doc_name": name, "display_name": name,
                "version": rec["version"] or ("" if not d.get("doc_version") else d["doc_version"]),
                "format": rec["format"] or format_of(d["doc_name"]),
                "size": rec["size"] or 0, "status": "done",
                "error": "", "created_at": rec["created_at"] or d.get("created_at", 0),
            }
        else:
            combined[d["doc_id"]] = {
                **d, "display_name": d["doc_name"], "version": "",
                "format": format_of(d["doc_name"]), "size": 0, "status": "done",
                "error": "", "created_at": d.get("created_at", 0),
            }
    # 仅登记表中、尚未入向量的 processing/failed 文档, 供前端展示状态与失败原因
    for rid, rec in rec_by_id.items():
        if rid not in combined and rec["status"] in ("processing", "failed"):
            combined[rid] = {
                "doc_id": rid,
                "doc_name": rec["display_name"] or rid,
                "display_name": rec["display_name"] or rid,
                "chunks": 0, "doc_version": None,
                "version": rec["version"], "format": rec["format"],
                "size": rec["size"], "status": rec["status"],
                "error": rec["error"], "created_at": rec["created_at"] or 0,
            }

    docs = _enrich_meta(list(combined.values()), tenant_id, user)

    if category:
        docs = [d for d in docs if d["category"] == category]
    if access_scope:
        docs = [d for d in docs if d["access_scope"] == access_scope]
    if status:
        docs = [d for d in docs if d["status"] == status]
    if search:
        kw = search.lower().strip()
        docs = [d for d in docs if kw in d["doc_name"].lower()
                or kw in d["display_name"].lower()]
    # 默认按上传/更新时间倒序
    docs.sort(key=lambda d: d.get("created_at") or 0, reverse=True)

    total = len(docs)
    total_chunks = sum(d["chunks"] for d in docs)
    if page is not None:
        per_page = per_page or 20
        start = (max(page, 1) - 1) * per_page
        docs = docs[start:start + per_page]
    return {"documents": docs, "total": total,
            "total_chunks": total_chunks,
            "page": page, "per_page": per_page, "pages": (total + per_page - 1) // per_page if per_page else 1}


@router.post("/import", summary="按路径批量导入/更新", dependencies=[Depends(write_required)])
def import_documents(req: ImportRequest, user: dict = Depends(write_required)) -> dict:
    if not req.paths:
        raise HTTPException(400, "paths 不能为空")
    results = _pipeline().ingest_paths(
        req.paths, tenant_id=current_tenant_id(user), created_by=user.get("username", "")
    )
    imported = [r for r in results if isinstance(r, dict) and "doc_id" in r]
    errors = [r for r in results if isinstance(r, dict) and "error" in r]
    audit("import", actor=user.get("username", ""), tenant_id=current_tenant_id(user),
          role=user.get("role", ""),
          detail=f"按路径导入 {len(imported)} 篇, 失败 {len(errors)} 篇",
          target=", ".join(req.paths)[:200])
    return {"imported": imported, "errors": errors,
            "imported_count": len(imported), "error_count": len(errors)}


@router.post("/upload", summary="上传文件并导入(支持 md/txt/pdf/docx)", dependencies=[Depends(write_required)])
def upload_documents(files: List[UploadFile] = File(...), user: dict = Depends(write_required)) -> dict:
    """上传一/多个文件入库。逐文件: 校验格式与大小 → 登记记录(通道后台记录) →
    解析+切片+向量化 → 标记 成功/失败(记录失败原因)。"""
    settings = get_settings()
    upload_dir = Path(settings.uploads_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = settings.upload_max_mb * 1024 * 1024
    tenant_id = current_tenant_id(user)
    created_by = user.get("username", "")
    rec_store = get_doc_record_store()
    pipeline = _pipeline()
    imported: List[dict] = []
    errors: List[dict] = []

    for f in files:
        fn = f.filename or "unnamed"
        ext = Path(fn).suffix.lower()
        if ext not in SUPPORTED_UPLOAD_EXTS:
            errors.append({"name": fn, "error": "格式不支持, 仅限 PDF / Word(docx) / TXT / Markdown"})
            continue
        data = f.file.read(max_bytes + 1)
        if len(data) > max_bytes:
            errors.append({"name": fn, "error": f"文件超过单文件 {settings.upload_max_mb}MB 上限"})
            continue
        dest = upload_dir / Path(fn).name
        with dest.open("wb") as out:
            out.write(data)
        try:
            r = pipeline.ingest_file(dest, tenant_id=tenant_id, created_by=created_by)
            if "error" in r:
                raise RuntimeError(r["error"])
            doc_id = r["doc_id"]
            rec_store.register(doc_id, tenant_id, r["doc_name"], format_of(fn),
                               len(data), str(dest))
            rec_store.set_status(doc_id, tenant_id, "done")
            audit("upload", actor=created_by, tenant_id=tenant_id, role=user.get("role", ""),
                  target=doc_id, detail=f"上传 {fn} → {r['chunks']} 块 / v{r['version']}")
            imported.append({**r, "status": "done"})
        except Exception as exc:  # noqa: BLE001
            doc_id = IngestPipeline.compute_doc_id(dest, dest.stem)
            rec_store.register(doc_id, tenant_id, fn, format_of(fn), len(data), str(dest))
            rec_store.set_status(doc_id, tenant_id, "failed", str(exc))
            audit("upload_failed", actor=created_by, tenant_id=tenant_id,
                  role=user.get("role", ""), target=doc_id,
                  detail=f"上传 {fn} 失败: {str(exc)[:200]}")
            errors.append({"name": fn, "error": str(exc), "doc_id": doc_id})

    # 知识库更新公告 → 通知中心广播
    if imported:
        try:
            _notify_kb_update(tenant_id,
                              f"{', '.join(i.get('doc_name', i.get('doc_id',''))[:20] for i in imported[:3])} 等 {len(imported)} 篇文档已更新")
        except Exception:  # noqa: BLE001
            pass

    return {"imported": imported, "errors": errors,
            "imported_count": len(imported), "error_count": len(errors)}


@router.post("/scan", summary="扫描目录批量导入(默认增量; background=true 后台执行)", dependencies=[Depends(write_required)])
def scan_documents(
    req: ScanRequest,
    background: bool = False,
    incremental: bool = True,
    user: dict = Depends(write_required),
) -> dict:
    settings = get_settings()
    directory = req.directory or settings.manuals_dir
    if not Path(directory).is_dir():
        raise HTTPException(404, f"目录不存在: {directory}")
    tenant_id = current_tenant_id(user)
    created_by = user.get("username", "")

    if background:
        placeholder = {"params": {"directory": directory, "incremental": incremental,
                                  "tenant_id": tenant_id, "created_by": created_by}}
        task = get_task_manager().submit("scan", lambda t: _run_scan_task({**t, **placeholder}))
        audit("scan", actor=created_by, tenant_id=tenant_id, role=user.get("role", ""),
              target=directory, detail=f"后台扫描导入(增量={incremental}) 任务 {task['id']}")
        return {"task_id": task["id"], "status": task["status"], "directory": directory,
                "progress_url": f"/api/v1/tasks/{task['id']}"}

    try:
        results = _pipeline().ingest_directory(
            directory, incremental=incremental, tenant_id=tenant_id, created_by=created_by
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    imported = [r for r in results if isinstance(r, dict) and "error" not in r]
    errors = [r for r in results if isinstance(r, dict) and "error" in r]
    audit("scan", actor=created_by, tenant_id=tenant_id, role=user.get("role", ""),
          target=directory, detail=f"扫描导入 {len(imported)} 篇, 失败 {len(errors)} 篇")
    return {"directory": directory, "imported": imported, "errors": errors,
            "imported_count": len(imported)}


@router.post("/web-import", summary="从互联网 URL 抓取文档导入(补充私有知识库)", dependencies=[Depends(write_required)])
def web_import(req: WebImportRequest, user: dict = Depends(write_required)) -> dict:
    if not req.urls:
        raise HTTPException(400, "urls 不能为空")
    settings = get_settings()
    downloads_dir = Path(settings.uploads_dir) / "web"
    tenant_id = current_tenant_id(user)
    created_by = user.get("username", "")

    def _do(task: dict) -> dict:
        pipeline = _pipeline()
        imported, errors = [], []
        for i, url in enumerate(req.urls, start=1):
            get_task_manager().update(task["id"], done=i - 1, total=len(req.urls), current=url)
            try:
                path = save_webpage(url, downloads_dir)
                imported.append(pipeline.ingest_file(
                    path, tenant_id=tenant_id, created_by=created_by
                ))
            except Exception as exc:  # noqa: BLE001
                errors.append({"url": url, "error": str(exc)})
        return {"imported": imported, "errors": errors,
                "imported_count": len(imported), "error_count": len(errors)}

    if req.background:
        task = get_task_manager().submit("web-import", _do)
        return {"task_id": task["id"], "status": task["status"],
                "progress_url": f"/api/v1/tasks/{task['id']}"}
    return _do({"id": "sync"})


def _purge_doc_sources(doc_id: str, tenant_id: str) -> None:
    """级联清理: 向量 + 版本 + 元数据 + 记录 + 源文件。"""
    rec = get_doc_record_store().get(doc_id, tenant_id)
    get_milvus_store().delete_doc(doc_id, tenant_id)
    get_version_store().delete_doc(doc_id)
    get_doc_meta_store().delete_doc(doc_id, tenant_id)
    get_doc_record_store().delete(doc_id, tenant_id)
    if rec and rec.get("source_path"):
        src = Path(rec["source_path"])
        try:
            if src.is_file():
                src.unlink(missing_ok=True)
        except OSError:  # noqa: BLE001
            pass


@router.post("/batch-delete", summary="批量删除文档及其向量", dependencies=[Depends(write_required)])
def batch_delete_documents(req: BatchDeleteRequest, user: dict = Depends(write_required)) -> dict:
    ids = [d for d in req.doc_ids if d]
    if not ids:
        raise HTTPException(400, "doc_ids 不能为空")
    tenant_id = current_tenant_id(user)
    deleted = []
    for doc_id in ids:
        _purge_doc_sources(doc_id, tenant_id)
        deleted.append(doc_id)
    audit("batch_delete", actor=user.get("username", ""), tenant_id=tenant_id,
          role=user.get("role", ""), detail=f"批量删除 {len(deleted)} 篇文档",
          target=", ".join(ids)[:200])
    return {"deleted": deleted, "count": len(deleted)}


@router.get("/stats", summary="每文档命中/覆盖统计(admin)", dependencies=[Depends(read_required)])
def documents_stats(user: dict = Depends(read_required)) -> dict:
    """聚合每文档: 知识块数、历史命中数、最近命中时间(用于知识库覆盖度分析)。"""
    from app.services.doc_stats import get_doc_stats_store

    tenant_id = current_tenant_id(user)
    docs = get_milvus_store().list_documents(tenant_id)
    by_id = {d["doc_id"]: d for d in docs}
    hits = get_doc_stats_store().stats(tenant_id)
    rows = []
    for h in hits:
        doc = by_id.pop(h["doc_id"], None)
        rows.append({
            "doc_id": h["doc_id"],
            "doc_name": h["doc_name"] or (doc or {}).get("doc_name", h["doc_id"]),
            "chunks": (doc or {}).get("chunks", 0),
            "hits": h["hit_count"],
            "last_hit_at": h["last_hit_at"],
        })
    # 从未被命中的文档也纳入(便于识别"零覆盖"资料)
    for d in by_id.values():
        rows.append({"doc_id": d["doc_id"], "doc_name": d["doc_name"],
                     "chunks": d["chunks"], "hits": 0, "last_hit_at": None})
    # 附加分类/权限并过滤不可见(非 admin 隐藏 private)
    rows = _enrich_meta(rows, tenant_id, user)
    rows.sort(key=lambda r: -r["hits"])
    total_chunks = sum(r["chunks"] for r in rows)
    covered_chunks = sum(r["chunks"] for r in rows if r["hits"] > 0)
    return {
        "documents": rows,
        "categories": get_doc_meta_store().categories(tenant_id),
        "total_documents": len(rows),
        "total_chunks": total_chunks,
        "covered_chunks": covered_chunks,
        "coverage_rate": round(covered_chunks / total_chunks, 4) if total_chunks else 0.0,
        "total_hits": sum(r["hits"] for r in rows),
    }


@router.get("/export-csv", summary="导出知识库文档清单为 CSV（含命中统计）", dependencies=[Depends(read_required)])
def export_documents_csv(user: dict = Depends(read_required)) -> Response:
    """把当前用户可见的文档清单(含分类/权限/命中统计)导出为 CSV 文件。"""
    from app.services.doc_stats import get_doc_stats_store

    tenant_id = current_tenant_id(user)
    docs = get_milvus_store().list_documents(tenant_id)
    by_id = {d["doc_id"]: d for d in docs}
    hits = get_doc_stats_store().stats(tenant_id)
    rows: List[dict] = []
    for h in hits:
        doc = by_id.pop(h["doc_id"], None)
        rows.append({"doc_id": h["doc_id"],
                     "doc_name": h["doc_name"] or (doc or {}).get("doc_name", h["doc_id"]),
                     "chunks": (doc or {}).get("chunks", 0), "hits": h["hit_count"],
                     "last_hit_at": h["last_hit_at"]})
    for d in by_id.values():
        rows.append({"doc_id": d["doc_id"], "doc_name": d["doc_name"],
                     "chunks": d["chunks"], "hits": 0, "last_hit_at": None})
    # 附加分类/权限并过滤不可见(复用 /stats 相同逻辑)
    rows = _enrich_meta(rows, tenant_id, user)
    rows.sort(key=lambda r: -r["hits"])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["doc_id", "doc_name", "format", "version", "category",
                     "access_scope", "chunks", "hits", "last_hit_at"])
    for r in rows:
        writer.writerow([
            r.get("doc_id", ""), r.get("doc_name", ""),
            format_of(r.get("doc_name", "") or ""), "",
            r.get("category", ""), r.get("access_scope", ""),
            r.get("chunks", 0), r.get("hits", 0), r.get("last_hit_at", ""),
        ])
    content = buf.getvalue()
    filename = f"knowledge_base_{tenant_id}_{_ts()}.csv"
    return Response(
        content="\ufeff" + content,  # BOM 便于 Excel 识别 UTF-8 中文
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.delete("/{doc_id}", summary="删除文档及其全部向量", dependencies=[Depends(write_required)])
def delete_document(doc_id: str, user: dict = Depends(write_required)) -> dict:
    tenant_id = current_tenant_id(user)
    _purge_doc_sources(doc_id, tenant_id)
    audit("delete", actor=user.get("username", ""), tenant_id=tenant_id,
          role=user.get("role", ""), target=doc_id, detail="删除文档及其全部向量/版本/元数据/源文件")
    return {"deleted": doc_id}


# ---------------------------------------------------------------------------
# 知识库增强: 分类 + 权限 + 批量元数据
# ---------------------------------------------------------------------------
@router.get("/categories", summary="知识分类清单(含各分类文档数)", dependencies=[Depends(read_required)])
def list_categories(user: dict = Depends(read_required)) -> dict:
    tenant_id = current_tenant_id(user)
    cats = get_doc_meta_store().categories(tenant_id)
    return {"categories": cats, "total": len(cats)}


@router.put("/{doc_id}/meta", summary="设置单篇文档的分类/可见权限", dependencies=[Depends(write_required)])
def update_doc_meta(
    doc_id: str, req: DocMetaUpdate, user: dict = Depends(write_required)
) -> dict:
    tenant_id = current_tenant_id(user)
    if not req.category and not req.access_scope:
        raise HTTPException(400, "至少提供 category 或 access_scope 之一")
    try:
        r = get_doc_meta_store().set_meta(
            doc_id, tenant_id, category=req.category,
            access_scope=req.access_scope, by=user.get("username", ""),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit("set_doc_meta", actor=user.get("username", ""), tenant_id=tenant_id,
          role=user.get("role", ""), target=doc_id,
          detail=f"分类={r['category'] or '(未设)'} 权限={r['access_scope']}")
    return r


@router.post("/batch-meta", summary="批量设置多篇文档的分类/可见权限", dependencies=[Depends(write_required)])
def batch_update_meta(req: BatchMetaRequest, user: dict = Depends(write_required)) -> dict:
    ids = [d for d in req.doc_ids if d]
    if not ids:
        raise HTTPException(400, "doc_ids 不能为空")
    if not req.category and not req.access_scope:
        raise HTTPException(400, "至少提供 category 或 access_scope 之一")
    tenant_id = current_tenant_id(user)
    try:
        n = get_doc_meta_store().set_meta_many(
            ids, tenant_id, category=req.category,
            access_scope=req.access_scope, by=user.get("username", ""),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit("batch_meta", actor=user.get("username", ""), tenant_id=tenant_id,
          role=user.get("role", ""), target=", ".join(ids)[:200],
          detail=f"批量更新 {n} 篇 (分类={req.category or '(不变)'} 权限={req.access_scope or '(不变)'})")
    return {"updated": n, "doc_ids": ids}


# ---------------------------------------------------------------------------
# 专门分类管理: 重命名 / 删除(含批量)。删除仅将分类内文档的 category 置空, 不删文档。
# ---------------------------------------------------------------------------
class RenameCategoryRequest(BaseModel):
    new_name: str


class DeleteCategoriesRequest(BaseModel):
    names: List[str]


@router.put("/categories/{category}", summary="重命名分类", dependencies=[Depends(write_required)])
def rename_category(category: str, req: RenameCategoryRequest, user: dict = Depends(write_required)) -> dict:
    tenant_id = current_tenant_id(user)
    try:
        n = get_doc_meta_store().rename_category(category, req.new_name, tenant_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit("rename_category", actor=user.get("username", ""), tenant_id=tenant_id,
          role=user.get("role", ""), target=category, detail=f"分类 {category} → {req.new_name.strip()} (影响 {n} 篇)")
    return {"renamed": n, "from": category, "to": req.new_name.strip()}


@router.post("/categories/delete", summary="删除分类(批量; 分类内文档移至未分类)", dependencies=[Depends(write_required)])
def delete_categories(req: DeleteCategoriesRequest, user: dict = Depends(write_required)) -> dict:
    names = [n for n in req.names if n and n.strip()]
    if not names:
        raise HTTPException(400, "names 不能为空")
    tenant_id = current_tenant_id(user)
    n = get_doc_meta_store().clear_categories(names, tenant_id)
    audit("delete_category", actor=user.get("username", ""), tenant_id=tenant_id,
          role=user.get("role", ""), target=", ".join(names)[:200],
          detail=f"删除分类 {len(names)} 个, 其中 {n} 篇文档移入未分类")
    return {"cleared": n, "deleted": names, "count": len(names)}


# ---------------------------------------------------------------------------
# 知识库管理 MVP: 详情 / 展示名 & 版本号编辑 / 覆盖上传(新版本) / 失败重试
# ---------------------------------------------------------------------------
@router.get("/{doc_id}/detail", summary="文档详情(基本信息 + 状态)", dependencies=[Depends(read_required)])
def document_detail(doc_id: str, user: dict = Depends(read_required)) -> dict:
    tenant_id = current_tenant_id(user)
    meta = _doc_meta(doc_id, tenant_id)
    _raise_unless_doc_viewable(doc_id, tenant_id, user, meta)
    rec = get_doc_record_store().get(doc_id, tenant_id)
    doc = None
    for d in get_milvus_store().list_documents(tenant_id):
        if d["doc_id"] == doc_id:
            doc = d
            break
    chunks = 0
    if doc:
        chunks = doc["chunks"]
    meta = get_doc_meta_store().get(doc_id, tenant_id)
    return {
        "doc_id": doc_id,
        "doc_name": (rec["display_name"] if rec and rec.get("display_name") else (doc or {}).get("doc_name", doc_id)),
        "display_name": (rec["display_name"] if rec else (doc or {}).get("doc_name", doc_id)),
        "version": rec["version"] if rec else (doc.get("doc_version") if doc else ""),
        "status": rec["status"] if rec else ("done" if doc else "missing"),
        "error": rec["error"] if rec else "",
        "format": rec["format"] if rec else format_of((doc or {}).get("doc_name", doc_id)),
        "size": rec["size"] if rec else 0,
        "chunks": chunks,
        "doc_version": (doc or {}).get("doc_version"),
        "created_at": (rec["created_at"] if rec else (doc or {}).get("created_at", 0)),
        "access_scope": meta.get("access_scope", "tenant"),
        "category": meta.get("category", ""),
    }


class DocProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    version: Optional[str] = None


@router.put("/{doc_id}/profile", summary="编辑展示名称 / 软件版本号", dependencies=[Depends(write_required)])
def update_doc_profile(doc_id: str, req: DocProfileUpdate, user: dict = Depends(write_required)) -> dict:
    import re

    if req.display_name is not None:
        req.display_name = req.display_name.strip()
    if req.version is not None:
        req.version = req.version.strip()
        if not re.fullmatch(r"v?\d+(\.\d+)*", req.version or ""):
            raise HTTPException(400, "版本号需为数字片段(可带 v 前缀), 如 1、1.0、v2.3.1")
    if (req.display_name is None or req.display_name == "") and \
       (req.version is None or req.version == ""):
        raise HTTPException(400, "至少提供 display_name 或 version 之一")
    tenant_id = current_tenant_id(user)
    store = get_doc_record_store()
    r = store.update_profile(doc_id, tenant_id,
                             display_name=req.display_name, version=req.version)
    audit("update_profile", actor=user.get("username", ""), tenant_id=tenant_id,
          role=user.get("role", ""), target=doc_id,
          detail=f"展示名={req.display_name or '(不变)'} 版本={req.version or '(不变)'}")
    return r


@router.post("/{doc_id}/replace", summary="覆盖上传(新版本): 删旧切片+向量后重新导入", dependencies=[Depends(write_required)])
def replace_document(doc_id: str, file: UploadFile = File(...), user: dict = Depends(write_required)) -> dict:
    """版本迭代: 用新文件替换同名文档。流程 = 旧文档清空(向量/版本/记录/源文件) →
    以新文件重新解析+切片+向量化(生成新版本号), 覆盖 upload 语义。"""
    settings = get_settings()
    upload_dir = Path(settings.uploads_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = settings.upload_max_mb * 1024 * 1024
    tenant_id = current_tenant_id(user)
    created_by = user.get("username", "")

    new_name = file.filename or "unnamed"
    ext = Path(new_name).suffix.lower()
    if ext not in SUPPORTED_UPLOAD_EXTS:
        raise HTTPException(400, "格式不支持, 仅限 PDF / Word(docx) / TXT / Markdown")
    data = file.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(400, f"文件超过单文件 {settings.upload_max_mb}MB 上限")

    # 旧文档: 目标 doc_id 固定为老文档 id, 先清空旧内容与源文件
    _purge_doc_sources(doc_id, tenant_id)

    dest = upload_dir / f"{doc_id}_{new_name}"
    with dest.open("wb") as out:
        out.write(data)
    try:
        # doc_id 强制为老 id, 使版本迭代保持文档身份不变(覆盖 = 同一文档出新版本)
        r = IngestPipeline().ingest_file(
            dest, doc_name=Path(new_name).stem, doc_id=doc_id,
            tenant_id=tenant_id, created_by=created_by,
        )
        if "error" in r:
            raise RuntimeError(r["error"])
        new_doc_id = r["doc_id"]
        rec_store = get_doc_record_store()
        rec_store.register(new_doc_id, tenant_id, r["doc_name"], format_of(new_name),
                           len(data), str(dest))
        rec_store.set_status(new_doc_id, tenant_id, "done")
        audit("overwrite", actor=created_by, tenant_id=tenant_id, role=user.get("role", ""),
              target=new_doc_id,
              detail=f"覆盖上传新版本 {new_name} → {r['chunks']} 块 / v{r['version']}")
        return {"doc_id": new_doc_id, "status": "done", "chunks": r["chunks"],
                "version": r["version"], "doc_name": r["doc_name"]}
    except Exception as exc:  # noqa: BLE001
        rec_store = get_doc_record_store()
        rec_store.register(doc_id, tenant_id, new_name, format_of(new_name), len(data), str(dest))
        rec_store.set_status(doc_id, tenant_id, "failed", str(exc))
        audit("overwrite_failed", actor=created_by, tenant_id=tenant_id,
              role=user.get("role", ""), target=doc_id, detail=f"覆盖上传失败: {str(exc)[:200]}")
        raise HTTPException(500, f"覆盖上传失败: {str(exc)}")


@router.post("/{doc_id}/retry", summary="失败文档重试(重新解析+切片+向量化)", dependencies=[Depends(write_required)])
def retry_document(doc_id: str, user: dict = Depends(write_required)) -> dict:
    """对 failed 记录重试: 若源文件仍存在则重新走入库流程; 否则提示需重新上传。"""
    tenant_id = current_tenant_id(user)
    rec_store = get_doc_record_store()
    rec = rec_store.get(doc_id, tenant_id)
    if not rec or rec["status"] not in ("failed", "processing"):
        raise HTTPException(400, "仅支持对失败/处理中的文档重试")
    src = Path(rec["source_path"] or "")
    if not src.is_file():
        raise HTTPException(409, "源文件已不存在, 请上传新文件重试(覆盖上传)")
    # 失败记录可能从未入库: 清空旧痕迹后重来
    get_milvus_store().delete_doc(doc_id, tenant_id)
    get_version_store().delete_doc(doc_id)
    rec_store.set_status(doc_id, tenant_id, "processing", "")
    try:
        r = IngestPipeline().ingest_file(
            src, doc_name=src.stem, doc_id=doc_id, tenant_id=tenant_id,
            created_by=user.get("username", ""),
        )
        if "error" in r:
            raise RuntimeError(r["error"])
        new_doc_id = r["doc_id"]
        rec_store.register(new_doc_id, tenant_id, r["doc_name"], rec.get("format", "") or format_of(str(src)),
                           rec.get("size", 0) or src.stat().st_size, str(src))
        rec_store.set_status(new_doc_id, tenant_id, "done")
        audit("retry", actor=user.get("username", ""), tenant_id=tenant_id,
              role=user.get("role", ""), target=new_doc_id,
              detail=f"重试成功 → {r['chunks']} 块 / v{r['version']}")
        if new_doc_id != doc_id:
            rec_store.delete(doc_id, tenant_id)
        return {"doc_id": new_doc_id, "status": "done", "chunks": r["chunks"],
                "version": r["version"]}
    except Exception as exc:  # noqa: BLE001
        rec_store.set_status(doc_id, tenant_id, "failed", str(exc))
        audit("retry_failed", actor=user.get("username", ""), tenant_id=tenant_id,
              role=user.get("role", ""), target=doc_id, detail=f"重试失败: {str(exc)[:200]}")
        raise HTTPException(500, f"重试失败: {str(exc)}")


@router.get("/{doc_id}/content", summary="获取知识库文档全文（用于知识库浏览器渲染）", dependencies=[Depends(read_required)])
def get_document_content(doc_id: str, user: dict = Depends(read_required)) -> dict:
    """返回 doc_id 对应的所有 chunk，按 chunk_index 排序，附带 doc_name/section_path/text。
    前端用于知识库浏览并定位引用章节。
    """
    tenant_id = current_tenant_id(user)
    _raise_unless_doc_viewable(doc_id, tenant_id, user, _doc_meta(doc_id, tenant_id))
    client = get_milvus_store().client
    expr = f'doc_id == "{doc_id}"'
    t = tenant_expr(tenant_id)
    if t:
        expr = f"{expr} and {t}"
    hits = client.query(
        collection_name=get_settings().milvus_collection,
        filter=expr,
        output_fields=["chunk_index", "section_path", "text", "doc_name", "page"],
        limit=10000,
    )
    if not hits:
        raise HTTPException(404, f"文档不存在: {doc_id}")
    first = hits[0] if hits else {}
    return {
        "doc_id": doc_id,
        "doc_name": first.get("doc_name", doc_id),
        "chunks": sorted(
            [
                {
                    "chunk_index": r["chunk_index"],
                    "section_path": r["section_path"] or "",
                    "text": r["text"],
                    "page": r["page"] or -1,
                }
                for r in hits
            ],
            key=lambda r: r["chunk_index"],
        ),
    }


@router.put("/{doc_id}/edit", summary="在线编辑文档全文并重新入库", dependencies=[Depends(write_required)])
def edit_document_content(doc_id: str, req: DocEditRequest, user: dict = Depends(write_required)) -> dict:
    """前端在线编辑后保存：把编辑后的全文写为临时 md 文件，复用 ingest_file(强制同 doc_id)
    重解析 + 重新切片 + 重新向量化 + 新版本快照，旧向量自动清除、BM25 同步更新。"""
    import tempfile

    tenant_id = current_tenant_id(user)
    _raise_unless_doc_viewable(doc_id, tenant_id, user, _doc_meta(doc_id, tenant_id))
    if not req.text or not req.text.strip():
        raise HTTPException(400, "文档内容不能为空")

    # 取当前展示名(无记录时回退 doc_id)
    doc_name = doc_id
    try:
        meta_rows = get_milvus_store().client.query(
            collection_name=get_settings().milvus_collection,
            filter=f'doc_id == "{doc_id}"',
            output_fields=["doc_name"], limit=1,
        )
        if meta_rows and meta_rows[0].get("doc_name"):
            doc_name = meta_rows[0]["doc_name"]
    except Exception:  # noqa: BLE001
        pass

    # 写临时 md 文件再走标准入库管线(保持编号/向量/版本/BM25 一致)
    fd, tmp_path = tempfile.mkstemp(suffix=".md", prefix="rag_edit_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(req.text)
        result = IngestPipeline().ingest_file(
            tmp_path, doc_name=doc_name, doc_id=doc_id,
            tenant_id=tenant_id, created_by=user.get("username", ""),
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:  # noqa: BLE001
            pass
    audit("edit_doc", actor=user.get("username", ""), tenant_id=tenant_id,
          role=user.get("role", ""), target=doc_id,
          detail=f"在线编辑并重新入库: {result.get('chunks', 0)} 块")
    return result


# ---------------------------------------------------------------------------
# 文档版本管理(修订历史 / 指定版本 / 回滚)
# ---------------------------------------------------------------------------
@router.get("/{doc_id}/versions", summary="文档修订历史列表", dependencies=[Depends(read_required)])
def doc_versions(doc_id: str, user: dict = Depends(read_required)) -> dict:
    tenant_id = current_tenant_id(user)
    _raise_unless_doc_viewable(doc_id, tenant_id, user, _doc_meta(doc_id, tenant_id))
    versions = get_version_store().list_versions(doc_id)
    if not versions:
        raise HTTPException(404, f"文档无版本记录: {doc_id}")
    return {"doc_id": doc_id, "count": len(versions),
            "versions": [
                {
                    "version": v["version"], "doc_name": v["doc_name"],
                    "chunk_count": v["chunk_count"], "source_file": v["source_file"],
                    "created_by": v.get("created_by", ""), "created_at": v["created_at"],
                }
                for v in versions
            ]}


@router.get("/{doc_id}/versions/{version}", summary="指定版本全文快照", dependencies=[Depends(read_required)])
def doc_version_detail(doc_id: str, version: int, user: dict = Depends(read_required)) -> dict:
    if version < 1:
        raise HTTPException(400, "版本号必须为正整数")
    tenant_id = current_tenant_id(user)
    _raise_unless_doc_viewable(doc_id, tenant_id, user, _doc_meta(doc_id, tenant_id))
    snap = get_version_store().get_version(doc_id, version)
    if not snap:
        raise HTTPException(404, f"版本不存在: {doc_id} v{version}")
    return {
        "doc_id": doc_id, "version": version, "doc_name": snap["doc_name"],
        "chunk_count": snap["chunk_count"], "created_at": snap["created_at"],
        "chunks": snap["chunks"],
    }


@router.post("/{doc_id}/versions/{version}/rollback", summary="回滚到指定版本(恢复为该文档当前激活)", dependencies=[Depends(write_required)])
def doc_version_rollback(doc_id: str, version: int, user: dict = Depends(write_required)) -> dict:
    """用指定版本快照重建当前激活向量(重新向量化覆盖 Milvus), 并生成新修订版本。"""
    if version < 1:
        raise HTTPException(400, "版本号必须为正整数")
    tenant_id = current_tenant_id(user)
    _raise_unless_doc_viewable(doc_id, tenant_id, user, _doc_meta(doc_id, tenant_id))
    snap = get_version_store().get_version(doc_id, version)
    if not snap:
        raise HTTPException(404, f"版本不存在: {doc_id} v{version}")
    chunks = snap["chunks"]
    if not chunks:
        raise HTTPException(400, "该版本无可用片段快照")

    pipeline = _pipeline()

    # 用快照文本重新向量化, 保持原 doc_id 与章节元数据
    import time

    texts = [c["text"] for c in chunks]
    vectors = pipeline.embedder.embed_documents(texts)
    now = int(time.time())
    rows = []
    for i, (c, vec) in enumerate(zip(chunks, vectors)):
        rows.append({
            "chunk_id": f"{doc_id}_{c['chunk_index']:05d}",
            "vector": vec,
            "text": c["text"][:8000],
            "doc_id": doc_id,
            "doc_name": snap["doc_name"][:500],
            "section_path": (c["section_path"] or "正文")[:1000],
            "chunk_index": c["chunk_index"],
            "page": c.get("page") if c.get("page", -1) >= 0 else -1,
            "tenant_id": tenant_id,
            "doc_version": 0,          # 占位, 下方经 save_version 生成新版本后回填
            "created_at": int(now),
        })
    # 回滚视为一次新修订: 指纹用时间戳使版本号递增
    fingerprint = f"rollback:{version}:{now}"
    new_ver = get_version_store().save_version(
        doc_id, snap["doc_name"], fingerprint, len(rows), snap["source_file"],
        [{"chunk_index": c["chunk_index"], "section_path": c["section_path"],
          "page": c.get("page") or -1, "text": c["text"]} for c in chunks],
        tenant_id, user.get("username", ""),
    )
    for r in rows:
        r["doc_version"] = new_ver
    pipeline.store.delete_doc(doc_id, tenant_id)
    pipeline.store.insert_chunks(rows)
    for r in rows:
        from app.core.bm25 import upsert_chunk

        upsert_chunk(r["chunk_id"], r["text"], tenant_id,
                     {k: r[k] for k in ("doc_id", "doc_name", "section_path",
                                        "chunk_index", "page", "tenant_id", "doc_version", "text")})
    return {"doc_id": doc_id, "rolled_back_to": version,
            "current_version": new_ver, "chunk_count": len(rows)}


# ---------------------------------------------------------------------------
# 目录树接口（用于知识库浏览器按本地文件结构浏览）
# ---------------------------------------------------------------------------
class DirectoryTreeNode(BaseModel):
    name: str
    path: str  # 相对根目录的路径
    is_dir: bool
    children: list["DirectoryTreeNode"] = []
    doc_count: int = 0  # 仅对目录有效，直接子文档数（不递归）
    doc_id: str = ""  # 仅文件节点有效，前端据此直接打开文档（与入库指纹一致）

class DirectoryTreeResponse(BaseModel):
    root_path: str
    children: list[DirectoryTreeNode]

@router.get("/directory-tree", summary="文件系统目录树（按原结构浏览知识库)", dependencies=[Depends(read_required)])
def list_directory_tree(user: dict = Depends(read_required)) -> DirectoryTreeResponse:
    """扫描 manuals_dir 目录，返回目录树结构，用于知识库浏览器导航。
    只包含支持的文档文件；第一级子目录将在 pipleline 导入时设置为分类。

    只展示「已入库」的文档：以 Milvus 知识库清单为准，避免批量删除后
    目录树仍残留原文件的旧标题（源文件可能因无 doc_record 记录而残留在磁盘）。
    """
    tenant_id = current_tenant_id(user)
    manuals_dir = Path(get_settings().manuals_dir)
    if not manuals_dir.is_dir():
        return DirectoryTreeResponse(root_path=str(manuals_dir), children=[])

    # 知识库权威清单: 只列入已向量化入库的 doc_id
    valid_ids = {d["doc_id"] for d in get_milvus_store().list_documents(tenant_id)}

    def scan(path: Path, base: Path) -> Optional[DirectoryTreeNode]:
        rel = str(path.relative_to(base))
        if path.is_file():
            doc_id = IngestPipeline.compute_doc_id(path, path.stem)
            if doc_id not in valid_ids:
                return None
            return DirectoryTreeNode(
                name=path.name,
                path=rel,
                is_dir=False,
                doc_count=0,
                doc_id=doc_id,
            )
        children = []
        doc_count = 0
        for p in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if p.is_dir() or p.suffix.lower() in SUPPORTED_EXTS:
                node = scan(p, base)
                if node is not None:
                    children.append(node)
                    if not node.is_dir:
                        doc_count += 1
        if path == base:
            # 根目录本身不作为一个目录节点返回(只透出其 children)
            return DirectoryTreeNode(name=base.name, path="", is_dir=True,
                                     children=children, doc_count=doc_count)
        return DirectoryTreeNode(
            name=path.name,
            path=rel,
            is_dir=True,
            children=children,
            doc_count=doc_count,
        )

    root = scan(manuals_dir, manuals_dir)
    return DirectoryTreeResponse(
        root_path=str(manuals_dir),
        children=(root.children if root else []),
    )
