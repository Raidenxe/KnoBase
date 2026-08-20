"""运行时动态配置层: 在静态 .env 配置之上提供可热覆盖的字段。

设计目的: 模型切换(LLM/Embedding)与检索参数(Top-K/阈值/RRf/Rerank)
无需重启即可生效, 同时保持"未覆盖时 = 静态默认值"的向后兼容。

- 所有字段初始值 = get_settings() 静态值(惰性初始化)
- set(field, value) / reset(field) / get(field, default) 均为线程安全
- snapshot() 输出当前全部动态覆盖, 供管理接口返回
- 不修改底层 Settings 对象, 其他读取点行为零变化
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from app.config import get_settings

# 允许动态覆盖的字段白名单(与原 config 字段同名)
OVERRIDABLE_FIELDS = frozenset({
    # LLM
    "llm_provider", "llm_base_url", "llm_api_key", "llm_model", "llm_temperature",
    # Embedding
    "embedding_provider", "fastembed_model",
    "embedding_api_base", "embedding_api_key", "embedding_model",
    # 检索
    "retrieval_top_k", "retrieval_score_threshold", "keyword_overlap_min",
    "max_context_chunks", "hybrid_bm25_k", "rrf_k",
    "rrf_vector_weight", "rrf_bm25_weight",
    "rerank_enabled", "rerank_top_k", "rerank_keep", "rerank_threshold",
    # 分块(入库切片; 仅对新导入/覆盖的文档生效)
    "chunk_size", "chunk_overlap",
})

_FIELD_TYPES = {
    "llm_provider": str, "llm_base_url": str, "llm_api_key": str,
    "llm_model": str, "llm_temperature": float,
    "embedding_provider": str, "fastembed_model": str,
    "embedding_api_base": str, "embedding_api_key": str, "embedding_model": str,
    "retrieval_top_k": int, "retrieval_score_threshold": float,
    "keyword_overlap_min": float, "max_context_chunks": int,
    "hybrid_bm25_k": int, "rrf_k": int,
    "rrf_vector_weight": float, "rrf_bm25_weight": float,
    "rerank_enabled": bool, "rerank_top_k": int, "rerank_keep": int,
    "rerank_threshold": float,
    "chunk_size": int, "chunk_overlap": int,
}


class RuntimeConfig:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._overrides: Dict[str, Any] = {}
        self._static: Optional[Dict[str, Any]] = None

    def _static_value(self, field: str) -> Any:
        if self._static is None:
            s = get_settings()
            self._static = {f: getattr(s, f) for f in OVERRIDABLE_FIELDS}
        return self._static.get(field)

    def get(self, field: str, default: Any = None) -> Any:
        with self._lock:
            if field in self._overrides:
                return self._overrides[field]
        return getattr(get_settings(), field, default)

    def set(self, field: str, value: Any) -> None:
        if field not in OVERRIDABLE_FIELDS:
            raise KeyError(f"字段 {field} 不支持运行时覆盖")
        if field in _FIELD_TYPES and value is not None:
            t = _FIELD_TYPES[field]
            if t is bool:
                value = str(value).lower() in ("1", "true", "yes", "on")
            else:
                value = t(value)
        with self._lock:
            self._overrides[field] = value

    def reset(self, field: str) -> None:
        with self._lock:
            self._overrides.pop(field, None)

    def is_overridden(self, field: str) -> bool:
        with self._lock:
            return field in self._overrides

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            snap = dict(self._overrides)
        return {f: snap[f] for f in OVERRIDABLE_FIELDS if f in snap}

    def clear(self) -> None:
        with self._lock:
            self._overrides.clear()


_store: Optional[RuntimeConfig] = None
_lock = threading.Lock()


def get_runtime_config() -> RuntimeConfig:
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                _store = RuntimeConfig()
    return _store