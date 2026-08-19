"""可选 Rerank 精排: 用 cross-encoder 对召回片段二次打分。

- 依赖 `sentence-transformers` / `torch`(可增量安装)。未安装或加载失败时,
  自动降级为"按原顺序返回", 不阻塞主检索链路。
- 精排分数写入每条的 `rerank_score` 字段, 供日志/评估观测。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_RERANKER_LOCK = threading.Lock()
_reranker = None
_RERANKER_LOADED = False


class _CrossEncoderWrapper:
    """懒加载 cross-encoder; 构建失败时置 failed=True, 后续调用直接降级。"""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.failed: Optional[str] = None
        self._model = None
        try:
            from sentence_transformers import CrossEncoder  # type: ignore
        except Exception as exc:  # noqa: BLE001
            self.failed = f"sentence-transformers 未安装: {exc}"
            logger.warning("Rerank 降级(未安装依赖): %s", self.failed)
            return
        try:
            self._model = CrossEncoder(model_name, max_length=512)
        except Exception as exc:  # noqa: BLE001
            self.failed = f"模型加载失败: {exc}"
            logger.warning("Rerank 降级(模型加载失败): %s", self.failed)

    def enabled(self) -> bool:
        return self._model is not None

    def rerank(self, query: str, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not self._model or not hits:
            return hits
        pairs = [(query, (h.get("text") or "")) for h in hits]
        try:
            scores = self._model.predict(pairs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Rerank 打分失败, 降级为原顺序: %s", exc)
            return hits
        for h, s in zip(hits, scores):
            h["rerank_score"] = round(float(s), 4)
        ordered = sorted(hits, key=lambda h: h.get("rerank_score", 0.0), reverse=True)
        return ordered


def get_reranker() -> "Optional[_CrossEncoderWrapper]":
    """按配置返回全局 reranker(仅 rerank_enabled 时构建), 失败自动降级。

    rerank_enabled 每次从动态配置层读取, 保证热切换开关即时生效;
    已加载的模型对象在开关关闭后仍保留, 重新开启时复用(避免重复加载)。
    """
    global _reranker, _RERANKER_LOADED
    from app.core.settings_rt import effective_settings

    if not effective_settings().rerank_enabled:
        return None
    if _RERANKER_LOADED:
        return _reranker
    with _RERANKER_LOCK:
        if _RERANKER_LOADED:
            return _reranker
        _reranker = _CrossEncoderWrapper(effective_settings().rerank_model)
        _RERANKER_LOADED = True
    return _reranker


def rerank_if_enabled(
    query: str, hits: List[Dict[str, Any]],
    settings: Any = None,
) -> List[Dict[str, Any]]:
    """入口: 按配置对候选做精排(可裁剪候选数), 未启用/不可用则原样返回。"""
    if not hits:
        return hits
    from app.core.settings_rt import effective_settings
    if settings is None:
        settings = effective_settings()
    if not settings.rerank_enabled:
        return hits
    reranker = get_reranker()
    if reranker is None or not reranker.enabled():
        return hits
    top_k = max(1, min(settings.rerank_top_k, len(hits)))
    keep = max(1, min(settings.rerank_keep or 0, len(hits)))
    ranked = reranker.rerank(query, hits[:top_k])
    threshold = getattr(settings, "rerank_threshold", 0.0) or 0.0
    if threshold > 0:
        # 丢弃重排分数低于阈值的片段, 避免低相关片段进入上下文
        before = len(ranked)
        ranked = [h for h in ranked if h.get("rerank_score", 0.0) >= threshold]
        logger.debug("rerank 阈值 %.3f: %d → %d", threshold, before, len(ranked))
    return ranked[:keep]