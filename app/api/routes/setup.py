"""初始化向导 API: 首次启动(认证开启且尚无管理员)时, 引导完成
「创建管理员 → 配置 LLM → 导入知识库」三步初始化。

安全性: bootstrap 仅允许在「尚无管理员」状态下调用(一次性), 创建后自动关闭。
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/setup", tags=["setup"])


def _auth_on() -> bool:
    return get_settings().auth_mode.lower() == "on"


def _users() -> list:
    from app.services.auth_store import get_auth_store

    return get_auth_store().list_users()


def setup_status() -> dict:
    admins = []
    if _auth_on():
        admins = [u for u in _users() if u.get("role") == "admin"]
    return {
        "auth_mode": "on" if _auth_on() else "off",
        "needs_setup": _auth_on() and not admins,
        "has_admin": bool(admins),
    }


class BootstrapRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8)
    display_name: str = ""
    llm_provider: str = "mock"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    import_kb: bool = True


@router.get("/status", summary="初始化向导状态(是否需要初始化)")
def status() -> dict:
    return setup_status()


@router.post("/bootstrap", summary="创建首个管理员并完成初始化(一次性)")
def bootstrap(req: BootstrapRequest) -> dict:
    if not _auth_on():
        raise HTTPException(400, "当前为演示模式(认证关闭), 无需初始化")
    if any(u.get("role") == "admin" for u in _users()):
        raise HTTPException(409, "已存在管理员, 初始化向导已关闭")

    from app.services.auth_store import get_auth_store, validate_password

    reason = validate_password(req.password)
    if reason:
        raise HTTPException(400, reason)
    if not req.llm_provider or req.llm_provider.lower() not in ("mock", "openai"):
        raise HTTPException(400, "LLM provider 仅支持: mock / openai")

    store = get_auth_store()
    tenant_id = get_settings().default_tenant
    try:
        user = store.create_user(
            req.username, req.password, tenant_id, "admin", req.display_name
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    _apply_llm(req)
    if req.import_kb:
        _start_import()

    return {
        "ok": True,
        "admin": {
            "id": user["id"], "username": user["username"],
            "role": user["role"], "tenant_id": user["tenant_id"],
        },
    }


def _apply_llm(req: BootstrapRequest) -> None:
    """写入运行时 LLM 配置, 立即生效(无需重启)。失败不阻断初始化。"""
    try:
        from app.core.llm import get_llm_service
        from app.core.runtime_config import get_runtime_config

        rt = get_runtime_config()
        rt.set("llm_provider", req.llm_provider.lower())
        if req.llm_base_url:
            rt.set("llm_base_url", req.llm_base_url)
        if req.llm_api_key:
            rt.set("llm_api_key", req.llm_api_key)
        if req.llm_model:
            rt.set("llm_model", req.llm_model)
        get_llm_service().reload()
    except Exception as exc:  # noqa: BLE001
        logger.warning("初始化阶段配置 LLM 失败(不影响管理员创建): %s", exc)


def _start_import() -> None:
    """后台触发知识库增量导入, 不阻塞初始化响应。"""
    from app.config import get_settings as _gs

    def _run() -> None:
        try:
            from app.knowledge.pipeline import IngestPipeline

            results = IngestPipeline().ingest_directory(
                _gs().manuals_dir, incremental=True
            )
            logger.info("初始化知识库导入完成: %d 篇文档更新", len(results))
        except Exception:  # noqa: BLE001
            logger.exception("初始化知识库导入失败")

    threading.Thread(target=_run, daemon=True, name="setup-import").start()