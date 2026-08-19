"""向量化提供者: 本地 fastembed(默认) / OpenAI 兼容 API / 哈希降级兜底。

统一接口:
    embed_documents(texts) -> list[vector]   文档入库向量化
    embed_query(text) -> vector              查询向量化
    dimension: int
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from abc import ABC, abstractmethod
from typing import List, Optional, Sequence

from app.config import get_settings

logger = logging.getLogger(__name__)


class BaseEmbedding(ABC):
    dimension: int = 0

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        ...

    @abstractmethod
    def embed_query(self, text: str) -> List[float]:
        ...

    @staticmethod
    def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)


class FastEmbedProvider(BaseEmbedding):
    """本地 ONNX 嵌入模型(bge-small-zh-v1.5, 中文检索优化, 完全离线推理)"""

    def __init__(self, model_name: str) -> None:
        import os

        from app.config import get_settings

        settings = get_settings()
        if settings.hf_endpoint:
            os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)
        if settings.hf_disable_xet:
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        from fastembed import TextEmbedding  # 延迟导入,加速启动

        self._model = TextEmbedding(model_name=model_name)
        self.dimension = len(next(iter(self._model.embed(["维度探测"]))))
        logger.info("FastEmbed 就绪: model=%s dim=%d", model_name, self.dimension)

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        return [e.tolist() for e in self._model.embed(list(texts))]

    def embed_query(self, text: str) -> List[float]:
        return next(iter(self._model.embed([text]))).tolist()


class OpenAIEmbeddingProvider(BaseEmbedding):
    """OpenAI 兼容 Embedding API"""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        from openai import OpenAI

        self._client = OpenAI(base_url=base_url or None, api_key=api_key)
        self._model = model
        self.dimension = len(
            self._client.embeddings.create(input=["维度探测"], model=model)
            .data[0]
            .embedding
        )
        logger.info("OpenAI Embedding 就绪: model=%s dim=%d", model, self.dimension)

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        out: List[List[float]] = []
        batch = 32
        for i in range(0, len(texts), batch):
            resp = self._client.embeddings.create(
                input=list(texts[i : i + batch]), model=self._model
            )
            out.extend(d.embedding for d in resp.data)
        return out

    def embed_query(self, text: str) -> List[float]:
        resp = self._client.embeddings.create(input=[text], model=self._model)
        return resp.data[0].embedding


class HashEmbeddingProvider(BaseEmbedding):
    """无网络/无模型时的确定性降级方案: 字符 n-gram 特征哈希。

    检索质量有限, 仅保证系统可用, 会记录告警。
    """

    def __init__(self, dimension: int = 512) -> None:
        self.dimension = dimension
        logger.warning("使用 Hash 降级嵌入(质量有限), 建议配置 fastembed 或 API")

    @staticmethod
    def _ngrams(text: str) -> List[str]:
        text = re.sub(r"\s+", "", text)
        return [text[i : i + 2] for i in range(max(len(text) - 1, 0))] or [text]

    def _vector(self, text: str) -> List[float]:
        vec = [0.0] * self.dimension
        for g in self._ngrams(text):
            h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
            vec[h % self.dimension] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._vector(text)


# ---------- 进程级单例(线程安全, 支持运行热切换) ----------
_embedder: Optional[BaseEmbedding] = None
_embedder_lock: threading.Lock = threading.RLock()


def _build(dynamic: bool = False) -> BaseEmbedding:
    """按(动态)配置构建嵌入提供者, 失败降级 Hash。"""
    from app.core.settings_rt import effective_settings

    eff = effective_settings() if dynamic else None
    settings = get_settings()
    provider = str(((eff.embedding_provider if eff and eff.is_overridden("embedding_provider") else settings.embedding_provider)) or settings.embedding_provider).lower()
    try:
        if provider == "openai":
            return OpenAIEmbeddingProvider(
                eff.embedding_api_base if eff and eff.is_overridden("embedding_api_base") else settings.embedding_api_base,
                eff.embedding_api_key if eff and eff.is_overridden("embedding_api_key") else settings.embedding_api_key,
                eff.embedding_model if eff and eff.is_overridden("embedding_model") else settings.embedding_model,
            )
        model = eff.fastembed_model if eff and eff.is_overridden("fastembed_model") else settings.fastembed_model
        return FastEmbedProvider(model)
    except Exception as exc:  # noqa: BLE001
        logger.error("初始化嵌入提供者 %s 失败: %s, 降级为 Hash 嵌入", provider, exc)
        return HashEmbeddingProvider()


def get_embedding_provider() -> BaseEmbedding:
    """进程级嵌入单例(线程安全)。正常路径返回静态实例;
    rebuild_embedder() 后会返回重建实例。"""
    global _embedder, _embedder_lock
    provider = _embedder
    if provider is None:
        with _embedder_lock:
            if _embedder is None:
                _embedder = _build(dynamic=False)
            provider = _embedder
    return provider


def rebuild_embedder(current_dimension: int | None = None) -> dict:
    """按当前(动态)Embedding 配置重建进程级嵌入单例, 支持运行热切换。

    维度与 Milvus 集合不一致时返回 dimension_changed=True, 由调用方提示
    "需重建 Milvus 集合" (沿用 scripts/migrate_schema.py), 不自动重建。
    """
    global _embedder
    new = _build(dynamic=True)
    with _embedder_lock:
        _embedder = new
    dimension_changed = current_dimension is not None and current_dimension != new.dimension
    return {
        "ok": True,
        "provider": new.__class__.__name__.replace("Provider", "").lower(),
        "dimension": new.dimension,
        "dimension_changed": dimension_changed,
    }
