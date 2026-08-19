"""知识库分类授权助手(基于 用户组 → 分类 的授权矩阵).

核心语义:
    - 授权关闭(演示模式 RAG_AUTH_MODE=off): 一律放行, 保持旧行为。
    - 生产模式(auth on):
        * 角色 admin / owner : 视为全部权限, 不设分类边界;
        * member / viewer    : 可读分类 = 其所属用户组具备 can_read 的分类并集,
                               可管分类 = can_manage 的并集。

返回值约定:
    category_scope(user) -> (readable, manageable):
        readable/manageable 为 None 表示"不限"; 否则为分类名列表(空列表=仅未分类/空分类文档可见)。
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

from app.core.security import auth_enabled
from app.services.auth_store import get_auth_store
from app.services.doc_meta import get_doc_meta_store

# 视为"全部权限"的角色(不经过分类授权过滤)
UNRESTRICTED_ROLES = {"admin", "owner"}


def category_scope(user: dict) -> Tuple[Optional[List[str]], Optional[List[str]]]:
    """返回当前用户的可读/可管分类范围; None 表示不限。"""
    if not auth_enabled() or user.get("is_anonymous"):
        return None, None
    if user.get("role") in UNRESTRICTED_ROLES:
        return None, None
    uid = user.get("id", "")
    if not uid:
        return None, None
    groups = [g["id"] for g in get_auth_store().user_groups(uid)]
    meta = get_doc_meta_store()
    return (
        meta.user_readable_categories(groups),
        meta.user_manageable_categories(groups),
    )


def readable_categories(user: dict) -> Optional[List[str]]:
    return category_scope(user)[0]


def manageable_categories(user: dict) -> Optional[List[str]]:
    return category_scope(user)[1]


def hits_category_scope(scope: Optional[List[str]], meta_map: "dict[str, dict]") -> set:
    """把用户可读分类收敛为"允许的 doc 命中必须落在其中的分类集合"。

    - scope is None      → 不限, 返回空集合(调用方按"不限制"处理)。
    - 空分类文档(未归类): 始终可读(与旧行为一致, 不因授权而锁死存量文档)。
    """
    if scope is None:
        return set()
    return {c for c in scope if c}


def filter_hits_by_scope(
    hits: List[dict], readable: Optional[List[str]], meta_by_doc: "dict[str, dict]"
) -> List[dict]:
    """按可读分类过滤检索命中的片段(问答链路后置过滤)。

    readable 为 None → 不过滤(全部权限)。
    否则仅保留 未分类文档 或 分类∈readable 的命中。
    """
    if readable is None:
        return hits
    allowed = set(readable)
    out: List[dict] = []
    for h in hits:
        meta = meta_by_doc.get(h.get("doc_id", ""), {})
        cat = meta.get("category", "") or ""
        if not cat or cat in allowed:
            out.append(h)
    return out