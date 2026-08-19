"""BM25 关键词索引(Okapi BM25, 纯 Python 离线实现)。

用于多路混合检索的"关键词路": 与向量检索(COSINE)并行召回, 再经 RRF 融合,
兼顾语义广度的同时提升精确命中的查准率。

分词策略(中文场景, 不引入 jieba 等重依赖):
    - ASCII 词元: 字母/数字/下划线/点/连字符 的连续串(小写化)
    - 中文汉字: 生成"重叠二元组"(bigram), 单字退化为其本身

索引随知识库变化增量维护:
    - 启动/首次访问时从 Milvus 全量构建(build_from_store)
    - 入库后 upsert_chunk / 删除后 remove_chunk
"""

from __future__ import annotations

import logging
import math
import re
import threading
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

_CJK_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*|[\u4e00-\u9fff]+")
_ASCII_LEAD = "abcdefghijklmnopqrstuvwxyz0123456789_-."


def tokenize(text: str) -> List[str]:
    """文本 → 词元列表(ASCII 词 + 中文重叠二元组)。"""
    tokens: List[str] = []
    for m in _CJK_TOKEN_RE.finditer(text.lower()):
        tok = m.group(0)
        if tok[0] in _ASCII_LEAD:
            tokens.append(tok)
            continue
        # 中文: 重叠二元组; 单字退化为本身
        chars = list(tok)
        if len(chars) == 1:
            tokens.append(tok)
        else:
            tokens.extend("".join(chars[i : i + 2]) for i in range(len(chars) - 1))
    return tokens


class BM25Index:
    """纯内存 Okapi BM25 索引(线程安全)。

    - add_many / add : 增量写入(按 chunk_id 幂等替换)
    - remove          : 删除单个 chunk
    - build_from_store: 从 Milvus 全量重建
    """

    def __init__(self, k1: Optional[float] = None, b: Optional[float] = None) -> None:
        settings = get_settings()
        self.k1 = k1 if k1 is not None else settings.bm25_k1
        self.b = b if b is not None else settings.bm25_b
        self._docs: Dict[str, Dict[str, Any]] = {}   # chunk_id -> {tf, dl, meta}
        self._tenant: Dict[str, str] = {}            # chunk_id -> tenant_id
        self._df: Dict[str, int] = defaultdict(int)  # term -> 包含它的文档数
        self._doc_count = 0
        self._total_dl = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 构建与维护
    # ------------------------------------------------------------------
    def reset(self) -> None:
        with self._lock:
            self._docs.clear()
            self._tenant.clear()
            self._df.clear()
            self._doc_count = 0
            self._total_dl = 0

    def add(self, chunk_id: str, text: str, tenant_id: Optional[str] = None,
            meta: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self.remove(chunk_id)  # 幂等替换
            tokens = tokenize(text or "")
            dl = len(tokens)
            tf = Counter(tokens)
            self._docs[chunk_id] = {"tf": tf, "dl": dl, "meta": meta or {}}
            self._tenant[chunk_id] = tenant_id or ""
            if dl > 0:
                self._doc_count += 1
                self._total_dl += dl
                for t in tf:
                    self._df[t] += 1

    def add_many(self, entries: List[Dict[str, Any]]) -> int:
        for e in entries:
            self.add(e["chunk_id"], e.get("text", ""), e.get("tenant_id"),
                     e)
        return len(entries)

    def remove(self, chunk_id: str) -> None:
        with self._lock:
            doc = self._docs.pop(chunk_id, None)
            self._tenant.pop(chunk_id, None)
            if not doc:
                return
            if doc["dl"] > 0:
                self._doc_count -= 1
                self._total_dl -= doc["dl"]
                for t in doc["tf"]:
                    self._df[t] -= 1
                    if self._df[t] <= 0:
                        self._df.pop(t, None)

    def build_from_store(self, tenant_id: Optional[str] = None) -> int:
        from app.core.milvus_store import get_milvus_store

        chunks = get_milvus_store().all_chunks(tenant_id=tenant_id)
        with self._lock:
            self.reset()
            for c in chunks:
                self._add_unlocked(c["chunk_id"], c.get("text", ""),
                                   c.get("tenant_id"), c)
        logger.info("BM25 索引重建完成: %d 个 chunk", len(chunks))
        return len(chunks)

    def _add_unlocked(self, chunk_id, text, tenant_id, meta) -> None:
        tokens = tokenize(text or "")
        dl = len(tokens)
        tf = Counter(tokens)
        self._docs[chunk_id] = {"tf": tf, "dl": dl, "meta": meta or {}}
        self._tenant[chunk_id] = tenant_id or ""
        if dl > 0:
            self._doc_count += 1
            self._total_dl += dl
            for t in tf:
                self._df[t] += 1

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def search(self, query: str, top_k: int = 12,
               tenant_id: Optional[str] = None,
               doc_version: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if self._doc_count == 0:
                self.build_from_store(tenant_id)
            if self._doc_count == 0:
                return []
            q_tf = Counter(tokenize(query or ""))
            if not q_tf:
                return []
            avgdl = self._total_dl / self._doc_count
            scored: List[tuple[float, str]] = []
            for cid, doc in self._docs.items():
                if tenant_id and self._tenant.get(cid) != tenant_id:
                    continue
                if doc_version is not None:
                    v = (doc.get("meta") or {}).get("doc_version")
                    try:
                        v = int(v) if v is not None else None
                    except (TypeError, ValueError):
                        v = None
                    if v != doc_version:
                        continue
                dl = doc["dl"]
                if dl == 0:
                    continue
                denom = self.k1 * (1 - self.b + self.b * dl / avgdl) if avgdl else self.k1
                score = 0.0
                tf = doc["tf"]
                for term, qw in q_tf.items():
                    tfn = tf.get(term, 0)
                    if not tfn:
                        continue
                    score += self._idf(term) * (tfn * (self.k1 + 1)) / (tfn + denom)
                if score > 0:
                    scored.append((score, cid))
            scored.sort(key=lambda x: x[0], reverse=True)
            out: List[Dict[str, Any]] = []
            for s, cid in scored[:top_k]:
                row = dict(self._docs[cid]["meta"])
                row.setdefault("chunk_id", cid)
                row["bm25_score"] = round(s, 4)
                out.append(row)
            return out

    def _idf(self, term: str) -> float:
        n = self._df.get(term, 0)
        return math.log(1 + (self._doc_count - n + 0.5) / (n + 0.5))

    @property
    def size(self) -> int:
        return len(self._docs)


_idx: Optional[BM25Index] = None
_idx_lock = threading.Lock()


def get_bm25_index() -> BM25Index:
    global _idx
    if _idx is None:
        with _idx_lock:
            if _idx is None:
                _idx = BM25Index()
    return _idx


def rebuild_from_store(tenant_id: Optional[str] = None) -> int:
    """全量重建 BM25 索引(启动/手动触发)。"""
    return get_bm25_index().build_from_store(tenant_id)


def upsert_chunk(chunk_id: str, text: str, tenant_id: Optional[str] = None,
                 meta: Optional[Dict[str, Any]] = None) -> None:
    get_bm25_index().add(chunk_id, text, tenant_id, meta)


def remove_chunk(chunk_id: str) -> None:
    get_bm25_index().remove(chunk_id)