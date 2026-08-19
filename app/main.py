"""RAG 智能助手 - FastAPI 应用入口

启动: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from starlette.responses import Response
from starlette.staticfiles import StaticFiles as _StaticFiles

from app import __version__
from app.api.routes import (
    categories,
    chat,
    conversations,
    documents,
    feedback,
    notifications,
    observability,
    project_docs,
    shares,
    tasks,
    tickets,
    ws,
)
from app.config import get_settings
from app.core.tracing import TracingMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    for d in (settings.data_dir, settings.manuals_dir, settings.uploads_dir,
              Path(settings.history_db_path).parent):
        Path(d).mkdir(parents=True, exist_ok=True)

    logger.info("初始化嵌入模型(首次运行将下载模型权重)...")
    from app.core.embeddings import get_embedding_provider

    embedder = get_embedding_provider()

    logger.info("初始化 Milvus 集合...")
    from app.core.milvus_store import get_milvus_store

    store = get_milvus_store()
    store.ensure_collection(embedder.dimension)

    if settings.auto_ingest_on_startup:
        # 后台增量导入: 不阻塞服务就绪; 未变化文档按指纹跳过
        import threading

        def _startup_ingest() -> None:
            from app.knowledge.pipeline import IngestPipeline

            try:
                results = IngestPipeline().ingest_directory(
                    settings.manuals_dir, incremental=True
                )
                if results:
                    logger.info("启动增量导入完成: %d 个文档更新", len(results))
            except Exception:  # noqa: BLE001
                logger.exception("启动导入失败(不影响服务运行)")

            # 同步目录分类与权限: 确保已有文档的元数据与目录结构一致
            try:
                from pathlib import Path
                from app.knowledge.loader import SUPPORTED_EXTS

                base = Path(settings.manuals_dir)
                if base.is_dir():
                    pipeline = IngestPipeline()
                    from app.services.doc_meta import get_doc_meta_store

                    meta_store = get_doc_meta_store()
                    tenant_id = settings.default_tenant
                    synced = 0
                    for p in sorted(base.rglob("*")):
                        if p.suffix.lower() not in SUPPORTED_EXTS:
                            continue
                        rel = p.parent.relative_to(base)
                        if rel == Path("."):
                            continue
                        category = rel.parts[0]
                        access_scope = "private" if category == "平台API" else None
                        doc_id = pipeline.compute_doc_id(p, p.stem)
                        # 仅更新: 若文档已存在于 Milvus 且元数据与目录预期不符
                        if doc_id not in {d["doc_id"] for d in pipeline.store.list_documents(tenant_id)}:
                            continue
                        cur = meta_store.get(doc_id, tenant_id)
                        if cur.get("category") != category or cur.get("access_scope") != (access_scope or "tenant"):
                            meta_store.set_meta(doc_id, tenant_id, category=category, access_scope=access_scope, by="system")
                            synced += 1
                    if synced:
                        logger.info("分类/权限同步完成: %d 篇文档已更新", synced)
            except Exception:  # noqa: BLE001
                logger.exception("分类/权限同步失败(不影响服务运行)")

        threading.Thread(target=_startup_ingest, daemon=True, name="startup-ingest").start()

    logger.info("启动资源水位监控采集线程...")
    from app.services.resource_monitor import start_resource_monitor

    start_resource_monitor()

    logger.info("%s v%s 就绪 (embedding dim=%d)", settings.app_name, __version__, embedder.dimension)
    yield

    from app.services.resource_monitor import get_resource_monitor

    get_resource_monitor().stop()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="基于 RAG 的软件维保问答系统: 检索增强 + 来源引用 + 多重防幻觉",
        lifespan=lifespan,
    )
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(TracingMiddleware)
    app.include_router(chat.router)
    app.include_router(documents.router)
    app.include_router(categories.router)
    app.include_router(conversations.router)
    app.include_router(tasks.router)
    app.include_router(observability.router)
    app.include_router(ws.router)
    app.include_router(feedback.router)
    app.include_router(shares.router)
    app.include_router(tickets.router)
    app.include_router(notifications.router)
    app.include_router(project_docs.router)

    @app.get("/s/{token}", include_in_schema=False, summary="短分享链接 → 只读分享页")
    async def short_share_redirect(token: str):
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url=f"/share-html/{token}")

    # 认证与多租户(RBAC): 仅 auth on 时挂载; off 保持旧行为(匿名 + 默认租户)
    if settings.auth_mode.lower() == "on":
        from app.api.routes import auth

        app.include_router(auth.router)

    # 知识库浏览器入口
    from fastapi.responses import FileResponse
    from pathlib import Path as _Path

    @app.get("/documents-browser", tags=["ui"], summary="知识库文档浏览器页面")
    async def documents_browser():
        browser = _Path(__file__).parent.parent / "static" / "browser.html"
        if browser.exists():
            return FileResponse(browser)
        return FileResponse(_Path(__file__).parent.parent / "static" / "index.html")

    @app.get("/api/v1/health", tags=["system"], summary="健康检查")
    async def health() -> dict:
        from app.core.embeddings import get_embedding_provider
        from app.core.llm import get_llm_service
        from app.core.milvus_store import get_milvus_store

        embedding = get_embedding_provider()
        llm = get_llm_service()
        components = {}
        try:
            chunks = get_milvus_store().count()
            components["milvus"] = {"status": "up", "chunks": chunks}
        except Exception as exc:  # noqa: BLE001
            components["milvus"] = {"status": "down", "error": str(exc)}
        components["embedding"] = {
            "status": "up",
            "provider": type(embedding).__name__.replace("Provider", "").lower(),
            "dimension": embedding.dimension,
        }
        components["llm"] = {"status": "up", "provider": llm.provider}
        healthy = all(c["status"] == "up" for c in components.values())
        return {"status": "healthy" if healthy else "degraded",
                "version": __version__, "components": components}

    static_dir = Path(__file__).resolve().parent.parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    class NoCacheStatic(_StaticFiles):
        """静态控制台/查询页等 HTML 一律 no-cache, 避免浏览器缓存旧版前端(用户组按钮等改动即时生效)。"""

        def __init__(self) -> None:
            super().__init__(directory=str(static_dir), html=True)

        async def get_response(self, path: str, scope) -> Response:
            resp: Response = await super().get_response(path, scope)
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return resp

    # 用户上传附件(工单等)静态下载
    Path(settings.uploads_dir).mkdir(parents=True, exist_ok=True)
    app.mount("/uploads", _StaticFiles(directory=settings.uploads_dir),
              name="uploads")

    app.mount("/", NoCacheStatic(), name="static")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)
