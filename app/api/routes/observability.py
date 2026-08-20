"""可观测性接口: 单次链路详情 / 基础指标汇总 / 最近评估摘要。

- traces 与 metrics: 读共享 TraceStore(内存环形缓冲 + trace.db), 零运行时开销
- eval/summary: 仅 admin, 返回最近一次评估结果摘要(由脚本写入 eval/report_*.json)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.config import get_settings
from app.core.security import admin_required
from app.services.trace_store import get_trace_store

router = APIRouter(prefix="/api/v1", tags=["observability"])


class PromptUpdateRequest(BaseModel):
    prompt: str = ""
    reset: bool = False


@router.get("/traces", summary="最近请求链路(按 trace_id 反序)", dependencies=[Depends(admin_required)])
def list_traces(limit: int = Query(20, ge=1, le=200), user: dict = Depends(admin_required)) -> dict:
    return {"traces": get_trace_store().recent(limit)}


@router.get("/traces/{trace_id}", summary="查看一次请求的全链路阶段耗时", dependencies=[Depends(admin_required)])
def get_trace(trace_id: str, user: dict = Depends(admin_required)) -> dict:
    rec = get_trace_store().get(trace_id)
    if not rec:
        raise HTTPException(404, f"trace 不存在: {trace_id}")
    return rec


@router.get("/metrics/basic", summary="分阶段 P50/P95/峰值 与 拒答/失败率汇总", dependencies=[Depends(admin_required)])
def metrics_basic(user: dict = Depends(admin_required)) -> dict:
    return get_trace_store().summary()


@router.get("/eval/summary", summary="最近一次评估摘要(admin)", tags=["evaluation"])
def eval_summary(user: dict = Depends(admin_required)) -> dict:
    """读取 eval/report_*.json 中最新一份评估报告(脚本生成, 不上报运行时开销)。"""
    report_dir = Path(get_settings().eval_dataset_path).resolve().parent
    reports = sorted(report_dir.glob("report_*.json"), reverse=True)
    if not reports:
        return {"has_report": False}
    try:
        with reports[0].open(encoding="utf-8") as f:
            return {"has_report": True, "file": reports[0].name, **json.load(f)}
    except Exception as exc:  # noqa: BLE001
        return {"has_report": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 第四批 P2 —— 后台运维
# ---------------------------------------------------------------------------
@router.get("/metrics/endpoints", summary="各接口 P99 延迟 / QPS / 请求量(中间件埋点)", dependencies=[Depends(admin_required)])
def endpoint_metrics(user: dict = Depends(admin_required)) -> dict:
    from app.services.metrics import get_endpoint_metrics

    return get_endpoint_metrics().stats()


@router.get("/admin/prompt", summary="查看当前生效的生成 System Prompt(admin)", dependencies=[Depends(admin_required)])
def get_prompt(user: dict = Depends(admin_required)) -> dict:
    from app.core.llm import GENERATE_SYSTEM
    from app.services.prompt_store import get_prompt_store

    state = get_prompt_store().get_state()
    return {
        **state,
        "builtin_prompt": GENERATE_SYSTEM,
        "effective_prompt": state["prompt"] or GENERATE_SYSTEM,
    }


@router.put("/admin/prompt", summary="热更新生成 System Prompt, 即时生效(admin)", dependencies=[Depends(admin_required)])
def update_prompt(req: PromptUpdateRequest, user: dict = Depends(admin_required)) -> dict:
    from app.services.audit import audit
    from app.services.prompt_store import get_prompt_store

    store = get_prompt_store()
    actor = user.get("username", "")
    if req.reset:
        store.reset(by=actor)
        audit("reset_prompt", actor=actor, tenant_id=user.get("tenant_id", ""),
              role=user.get("role", ""), detail="恢复内置默认生成提示词")
        return {"customized": False, "message": "已恢复内置默认提示词"}
    if not (req.prompt or "").strip():
        raise HTTPException(400, "提示词不能为空")
    store.set_generate(req.prompt, by=actor)
    audit("update_prompt", actor=actor, tenant_id=user.get("tenant_id", ""),
          role=user.get("role", ""), detail=f"提示词已更新({len(req.prompt)}字)")
    return {"customized": True, "message": "已保存并即时生效(当前请求无需重启)"}


@router.get("/audit", summary="敏感操作审计日志(admin)", dependencies=[Depends(admin_required)])
def audit_logs(limit: int = Query(50, ge=1, le=500), target: Optional[str] = None,
               user: dict = Depends(admin_required)) -> dict:
    from app.services.audit import get_audit_store

    store = get_audit_store()
    logs = store.recent_for(target, min(limit, 500)) if target else store.recent(limit)
    return {"logs": logs, "total": len(logs)}


@router.get("/metrics/usage", summary="LLM token 消耗与成本核算(admin)", dependencies=[Depends(admin_required)])
def llm_usage(hours: int = Query(24, ge=1, le=720), user: dict = Depends(admin_required)) -> dict:
    from app.services.llm_usage import get_llm_usage_store

    return get_llm_usage_store().summary(hours)


# ---------------------------------------------------------------------------
# 运维四件套 —— 动态配置 / 模型切换 / 资源水位 / 慢请求拆分
# ---------------------------------------------------------------------------
class LLMModelUpdate(BaseModel):
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    temperature: float | None = None


class EmbeddingModelUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    api_base: str | None = None
    api_key: str | None = None
    embedding_model: str | None = None


class RetrievalUpdate(BaseModel):
    retrieval_top_k: int | None = None
    retrieval_score_threshold: float | None = None
    keyword_overlap_min: float | None = None
    max_context_chunks: int | None = None
    hybrid_bm25_k: int | None = None
    rrf_k: int | None = None
    rrf_vector_weight: float | None = None
    rrf_bm25_weight: float | None = None
    rerank_enabled: bool | None = None
    rerank_top_k: int | None = None
    rerank_keep: int | None = None
    rerank_threshold: float | None = None
    chunk_size: int | None = None
    chunk_overlap: int | None = None


_RETRIEVAL_KEYS = (
    "retrieval_top_k", "retrieval_score_threshold", "keyword_overlap_min",
    "max_context_chunks", "hybrid_bm25_k", "rrf_k",
    "rrf_vector_weight", "rrf_bm25_weight",
    "rerank_enabled", "rerank_top_k", "rerank_keep", "rerank_threshold",
    "chunk_size", "chunk_overlap",
)

_LLM_KEYS = ("llm_provider", "llm_base_url", "llm_api_key", "llm_model", "llm_temperature")
_EMBED_KEYS = ("embedding_provider", "fastembed_model", "embedding_api_base",
               "embedding_api_key", "embedding_model")


def _current_models() -> dict:
    from app.core.embeddings import get_embedding_provider
    from app.core.llm import get_llm_service
    from app.core.settings_rt import effective_settings
    from app.core.runtime_config import get_runtime_config

    settings = get_settings()
    eff = effective_settings()
    rt = get_runtime_config()
    llm_svc = get_llm_service()
    embedder = get_embedding_provider()
    return {
        "llm": {
            "provider": eff.llm_provider or settings.llm_provider,
            "base_url": eff.llm_base_url or settings.llm_base_url,
            "api_key": eff.llm_api_key or settings.llm_api_key,
            "model": eff.llm_model or settings.llm_model,
            "temperature": eff.llm_temperature or settings.llm_temperature,
            "effective_provider": llm_svc.provider,   # openai / mock (可能回退)
            "is_mock": llm_svc.is_mock,
        },
        "embedding": {
            "provider": eff.embedding_provider or settings.embedding_provider,
            "model": eff.fastembed_model or settings.fastembed_model,
            "api_base": eff.embedding_api_base or settings.embedding_api_base,
            "api_key": eff.embedding_api_key or settings.embedding_api_key,
            "embedding_model": eff.embedding_model or settings.embedding_model,
            "dimension": embedder.dimension,
        },
        "overridden": rt.snapshot(),
    }


@router.get("/admin/models", summary="当前 LLM / Embedding 动态配置(admin)", dependencies=[Depends(admin_required)])
def get_models(user: dict = Depends(admin_required)) -> dict:
    return _current_models()


@router.put("/admin/models/llm", summary="运行时切换 LLM 模型, 立即生效(admin)", dependencies=[Depends(admin_required)])
def update_llm(req: LLMModelUpdate, user: dict = Depends(admin_required)) -> dict:
    from app.core.runtime_config import get_runtime_config
    from app.core.llm import get_llm_service
    from app.services.audit import audit

    # 输入校验: provider 白名单 + temperature 范围
    if req.provider is not None and req.provider.lower() not in ("mock", "openai"):
        raise HTTPException(400, "LLM provider 仅支持: mock / openai")
    if req.temperature is not None and not (0.0 <= req.temperature <= 2.0):
        raise HTTPException(400, "temperature 需在 [0, 2] 之间")

    rt = get_runtime_config()
    payload = req.model_dump(exclude_none=True)
    if req.provider is not None or req.base_url is not None or req.api_key is not None:
        if req.provider is not None:
            rt.set("llm_provider", req.provider)
        if req.base_url is not None:
            rt.set("llm_base_url", req.base_url)
        if req.api_key is not None:
            rt.set("llm_api_key", req.api_key)
    if req.model is not None:
        rt.set("llm_model", req.model)
    if req.temperature is not None:
        rt.set("llm_temperature", req.temperature)

    get_llm_service().reload()
    audit("switch_llm", actor=user.get("username", ""), tenant_id=user.get("tenant_id", ""),
          role=user.get("role", ""), detail=f"切换 LLM: {payload.get('model')} / {payload.get('provider')}")
    return {"ok": True, **(_current_models()["llm"])}


@router.put("/admin/models/embedding", summary="运行时切换 Embedding 模型, 立即生效(admin)", dependencies=[Depends(admin_required)])
def update_embedding(req: EmbeddingModelUpdate, user: dict = Depends(admin_required)) -> dict:
    from app.core.embeddings import rebuild_embedder
    from app.core.milvus_store import get_milvus_store
    from app.core.runtime_config import get_runtime_config
    from app.services.audit import audit

    rt = get_runtime_config()
    # 输入校验: embedding provider 白名单
    if req.provider is not None and req.provider.lower() not in ("fastembed", "openai"):
        raise HTTPException(400, "Embedding provider 仅支持: fastembed / openai")
    if req.provider is not None:
        rt.set("embedding_provider", req.provider)
    if req.model is not None:
        rt.set("fastembed_model", req.model)
    if req.api_base is not None:
        rt.set("embedding_api_base", req.api_base)
    if req.api_key is not None:
        rt.set("embedding_api_key", req.api_key)
    if req.embedding_model is not None:
        rt.set("embedding_model", req.embedding_model)

    # 当前 Milvus 集合维度碰撞检测
    current_dim = None
    try:
        current_dim = get_milvus_store().dimension
    except Exception:  # noqa: BLE001
        current_dim = None
    result = rebuild_embedder(current_dim)
    audit("switch_embedding", actor=user.get("username", ""), tenant_id=user.get("tenant_id", ""),
          role=user.get("role", ""), detail=f"切换 Embedding: {req.model or req.provider}")
    return {"ok": True, **result}


@router.get("/admin/retrieval", summary="当前检索参数(含动态覆盖标记)(admin)", dependencies=[Depends(admin_required)])
def get_retrieval(user: dict = Depends(admin_required)) -> dict:
    from app.core.settings_rt import effective_settings
    from app.core.runtime_config import get_runtime_config

    settings = get_settings()
    eff = effective_settings()
    rt = get_runtime_config()
    params = {}
    for k in _RETRIEVAL_KEYS:
        value = getattr(eff, k)              # effective_settings 透明转发, 覆盖优先
        default = getattr(settings, k)
        params[k] = {
            "value": value,
            "default": default,
            "overridden": rt.is_overridden(k),
        }
    return {"params": params}


@router.put("/admin/retrieval", summary="运行时调整检索参数, 即时生效(admin)", dependencies=[Depends(admin_required)])
def update_retrieval(req: RetrievalUpdate, user: dict = Depends(admin_required)) -> dict:
    from app.core.runtime_config import get_runtime_config
    from app.services.audit import audit

    rt = get_runtime_config()
    payload = req.model_dump(exclude_none=True)
    for k in _RETRIEVAL_KEYS:
        if k in payload:
            if payload[k] is None:
                rt.reset(k)
            else:
                rt.set(k, payload[k])
    audit("update_retrieval", actor=user.get("username", ""), tenant_id=user.get("tenant_id", ""),
          role=user.get("role", ""), detail=f"调整检索参数: {payload}")
    return {"ok": True, "message": "检索参数已即时生效(下一请求即用新值)"}


@router.get("/admin/resource/current", summary="当前资源水位(CPU/内存/磁盘)(admin)", dependencies=[Depends(admin_required)])
def resource_current(user: dict = Depends(admin_required)) -> dict:
    from app.services.resource_monitor import get_resource_monitor

    return get_resource_monitor().current()


@router.get("/admin/resource/series", summary="资源水位历史曲线(24h)(admin)", dependencies=[Depends(admin_required)])
def resource_series(hours: int = Query(24, ge=1, le=72),
                    step: int = Query(60, ge=10, le=3600),
                    user: dict = Depends(admin_required)) -> dict:
    from app.services.resource_monitor import get_resource_monitor

    return get_resource_monitor().series(hours=hours, step=step)


@router.get("/admin/slow-requests", summary="最近慢请求列表(admin)", dependencies=[Depends(admin_required)])
def slow_requests(limit: int = Query(20, ge=1, le=100),
                  user: dict = Depends(admin_required)) -> dict:
    from app.services.slow_requests import get_slow_request_store

    return {"requests": get_slow_request_store().recent(limit)}


@router.get("/traces/{trace_id}/detail", summary="单请求耗时拆分明细(用于慢请求分解面板)", dependencies=[Depends(admin_required)])
def trace_detail(trace_id: str, user: dict = Depends(admin_required)) -> dict:
    rec = get_trace_store().get(trace_id)
    if not rec:
        raise HTTPException(404, f"trace 不存在: {trace_id}")
    spans = rec.get("spans", [])
    total_ms = sum(s.get("duration_ms", 0) for s in spans)
    return {"trace_id": trace_id, "status": rec.get("status"),
            "spans": spans, "total_ms": total_ms, "created_at": rec.get("started_at")}