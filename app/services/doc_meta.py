"""知识库文档元数据(SQLite kb_meta.db): 分类 + 访问权限。

为什么不用 Milvus 存这两项:
    - Milvus 为固定 schema(无动态字段), 加字段需 migrate_schema.py --force 重建集合并重新向量化既有文档, 成本高且有风险;
    - 分类/权限属于低频更新的"管理元数据", 与版本/审计/提示词一致走 SQLite 更轻量、零迁移。

提供能力:
    - category    : 知识分类(自由字符串, 可由管理员批量归类)
    - access_scope: 文档级可见权限, 取值为 tenant(本租户可见,默认) | private(仅管理员可见)
                    检索仍以租户隔离(RAG 安全底线), 此字段用于知识库管理/浏览层的可见性控制。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.config import get_settings

_LOCK = threading.RLock()

ACCESS_SCOPES = {"tenant", "private"}
DEFAULT_SCOPE = "tenant"


class DocMetaStore:
    def __init__(self, db_path: str) -> None:
        from pathlib import Path

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kb_docs(
                    doc_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    category TEXT DEFAULT '',
                    access_scope TEXT DEFAULT 'tenant',
                    updated_by TEXT DEFAULT '',
                    updated_at REAL,
                    PRIMARY KEY(doc_id, tenant_id)
                );
                CREATE INDEX IF NOT EXISTS idx_kbdoc_tenant ON kb_docs(tenant_id, category);
                -- 知识库分类(显式定义, P0 分类管理)
                CREATE TABLE IF NOT EXISTS categories(
                    name TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    created_by TEXT DEFAULT '',
                    created_at REAL,
                    updated_at REAL,
                    PRIMARY KEY(name, tenant_id)
                );
                CREATE INDEX IF NOT EXISTS idx_cat_tenant ON categories(tenant_id);
                -- 组×分类 授权矩阵
                CREATE TABLE IF NOT EXISTS category_grants(
                    group_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    can_read INTEGER DEFAULT 0,
                    can_manage INTEGER DEFAULT 0,
                    updated_by TEXT DEFAULT '',
                    updated_at REAL,
                    PRIMARY KEY(group_id, category)
                );
                CREATE INDEX IF NOT EXISTS idx_grant_group ON category_grants(group_id);
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    def get(self, doc_id: str, tenant_id: str) -> Dict[str, Any]:
        with _LOCK:
            row = self._conn.execute(
                "SELECT doc_id, category, access_scope, updated_by, updated_at "
                "FROM kb_docs WHERE doc_id=? AND tenant_id=?",
                (doc_id, tenant_id),
            ).fetchone()
        if not row:
            return {"doc_id": doc_id, "category": "", "access_scope": DEFAULT_SCOPE}
        return {
            "doc_id": row[0], "category": row[1] or "",
            "access_scope": row[2] or DEFAULT_SCOPE,
            "updated_by": row[3] or "", "updated_at": row[4],
        }

    def set_meta(
        self, doc_id: str, tenant_id: str,
        category: Optional[str] = None,
        access_scope: Optional[str] = None,
        by: str = "",
    ) -> Dict[str, Any]:
        if access_scope is not None and access_scope not in ACCESS_SCOPES:
            raise ValueError(f"非法的访问权限: {access_scope}, 可选: {sorted(ACCESS_SCOPES)}")
        cur = self.get(doc_id, tenant_id)
        new_cat = category if category is not None else cur["category"]
        new_scope = access_scope if access_scope is not None else cur["access_scope"]
        now = time.time()
        with _LOCK:
            self._conn.execute(
                "INSERT INTO kb_docs(doc_id, tenant_id, category, access_scope, updated_by, updated_at) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(doc_id, tenant_id) DO UPDATE SET "
                "category=excluded.category, access_scope=excluded.access_scope, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (doc_id, tenant_id, (new_cat or ""), new_scope, by, now),
            )
            self._conn.commit()
        return {"doc_id": doc_id, "category": new_cat or "", "access_scope": new_scope}

    def set_meta_many(
        self, doc_ids: List[str], tenant_id: str,
        category: Optional[str] = None,
        access_scope: Optional[str] = None,
        by: str = "",
    ) -> int:
        if access_scope is not None and access_scope not in ACCESS_SCOPES:
            raise ValueError(f"非法的访问权限: {access_scope}")
        now = time.time()
        n = 0
        update_cat = category is not None
        update_scope = access_scope is not None
        with _LOCK:
            for doc_id in doc_ids:
                cur = self.get(doc_id, tenant_id)
                cat = category if update_cat else cur["category"]
                scope = access_scope if update_scope else cur["access_scope"]
                self._conn.execute(
                    "INSERT INTO kb_docs(doc_id, tenant_id, category, access_scope, updated_by, updated_at) "
                    "VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(doc_id, tenant_id) DO UPDATE SET "
                    "category=excluded.category, access_scope=excluded.access_scope, "
                    "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (doc_id, tenant_id, cat or "", scope, by, now),
                )
                n += 1
            self._conn.commit()
        return n

    def get_many_meta(self, doc_ids: List[str], tenant_id: str) -> Dict[str, Dict[str, Any]]:
        by_id: Dict[str, str] = {}
        for i, d in enumerate(doc_ids):
            if d not in by_id:
                by_id[d] = d
        out: Dict[str, Dict[str, Any]] = {}
        if not doc_ids:
            return out
        placeholders = ",".join("?" for _ in doc_ids)
        with _LOCK:
            rows = self._conn.execute(
                f"SELECT doc_id, category, access_scope FROM kb_docs "
                f"WHERE doc_id IN ({placeholders})",
                list(doc_ids),
            ).fetchall()
        found = {r[0]: {"category": r[1] or "", "access_scope": r[2] or DEFAULT_SCOPE} for r in rows}
        for d in doc_ids:
            out[d] = found.get(d, {"category": "", "access_scope": DEFAULT_SCOPE})
        return out

    def categories(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT category, COUNT(*) FROM kb_docs WHERE category <> '' "
        args: Tuple = ()
        if tenant_id:
            sql += "AND tenant_id=? "
            args = (tenant_id,)
        sql += "GROUP BY category ORDER BY COUNT(*) DESC"
        with _LOCK:
            rows = self._conn.execute(sql, args).fetchall()
        return [{"category": r[0], "count": r[1]} for r in rows]

    def visible_means(
        self, tenant_id: str
    ) -> Dict[str, Dict[str, Any]]:
        """本租户内非 private(=tenant 可见) 的文档 doc_id -> meta。"""
        with _LOCK:
            rows = self._conn.execute(
                "SELECT doc_id, category, access_scope FROM kb_docs WHERE tenant_id=?",
                (tenant_id,),
            ).fetchall()
        return {r[0]: {"category": r[1] or "", "access_scope": r[2] or DEFAULT_SCOPE} for r in rows}

    def delete_doc(self, doc_id: str, tenant_id: str) -> None:
        with _LOCK:
            self._conn.execute(
                "DELETE FROM kb_docs WHERE doc_id=? AND tenant_id=?", (doc_id, tenant_id)
            )
            self._conn.commit()

    def rename_category(self, old_name: str, new_name: str, tenant_id: str) -> int:
        """重命名分类: 同步更新文档、显式分类表与授权矩阵, 返回受影响文档数。"""
        old = (old_name or "").strip()
        new = (new_name or "").strip()
        if not old:
            raise ValueError("旧分类名不能为空")
        if not new:
            raise ValueError("新分类名不能为空")
        with _LOCK:
            cur = self._conn.execute(
                "UPDATE kb_docs SET category=? WHERE category=? AND tenant_id=?",
                (new, old, tenant_id),
            )
            # 同步显式分类表(若原分类是显式定义)与授权矩阵
            if self._conn.execute(
                "SELECT 1 FROM categories WHERE name=? AND tenant_id=?", (old, tenant_id)
            ).fetchone():
                try:
                    self._conn.execute(
                        "UPDATE categories SET name=? WHERE name=? AND tenant_id=?",
                        (new, old, tenant_id),
                    )
                except sqlite3.IntegrityError:
                    pass
            self._conn.execute(
                "UPDATE category_grants SET category=? WHERE category=?", (new, old)
            )
            self._conn.commit()
            return cur.rowcount

    def clear_categories(self, names: List[str], tenant_id: str) -> int:
        """删除分类(安全): 将分类内文档的 category 置空, 不删除文档本身, 返回受影响文档数。"""
        names = [n for n in (names or []) if n and str(n).strip()]
        if not names:
            return 0
        ph = ",".join("?" for _ in names)
        with _LOCK:
            cur = self._conn.execute(
                f"UPDATE kb_docs SET category='' "
                f"WHERE category IN ({ph}) AND tenant_id=?",
                (*names, tenant_id),
            )
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # 知识库分类(显式定义) CRUD — P0 分类管理
    # ------------------------------------------------------------------
    def get_category(self, name: str, tenant_id: str) -> Optional[Dict[str, Any]]:
        with _LOCK:
            row = self._conn.execute(
                "SELECT name, tenant_id, description, created_by, created_at, updated_at "
                "FROM categories WHERE name=? AND tenant_id=?",
                (name, tenant_id),
            ).fetchone()
        if not row:
            return None
        return {
            "name": row[0], "tenant_id": row[1], "description": row[2] or "",
            "created_by": row[3] or "", "created_at": row[4], "updated_at": row[5],
        }

    def create_category(self, name: str, tenant_id: str, description: str = "", by: str = "") -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("分类名不能为空")
        now = time.time()
        with _LOCK:
            try:
                self._conn.execute(
                    "INSERT INTO categories(name,tenant_id,description,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (name, tenant_id, description or "", by, now, now),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"分类已存在: {name}") from exc
        return self.get_category(name, tenant_id) or {}  # type: ignore[return-value]

    def update_category(self, name: str, tenant_id: str, description: Optional[str] = None) -> Dict[str, Any]:
        cur = self.get_category(name, tenant_id)
        if not cur:
            raise ValueError(f"分类不存在: {name}")
        new_desc = description if description is not None else cur["description"]
        with _LOCK:
            self._conn.execute(
                "UPDATE categories SET description=?, updated_at=? WHERE name=? AND tenant_id=?",
                (new_desc, time.time(), name, tenant_id),
            )
            self._conn.commit()
        return self.get_category(name, tenant_id) or {}  # type: ignore[return-value]

    def delete_category(self, name: str, tenant_id: str) -> Dict[str, Any]:
        """删除分类: 若分类下仍有文档则抛错保留; 成功时级联清理该分类的授权矩阵与文档分类关联。"""
        _name = (name or "").strip()
        if not _name:
            raise ValueError("分类名不能为空")
        with _LOCK:
            cnt = self._conn.execute(
                "SELECT COUNT(*) FROM kb_docs WHERE category=? AND tenant_id=?",
                (_name, tenant_id),
            ).fetchone()
            if cnt and int(cnt[0]) > 0:
                raise ValueError(f"分类 {_name} 下仍有 {int(cnt[0])} 篇文档, 请先将文档移出或删除")
            # 清空文档分类 + 授权矩阵引用
            self._conn.execute(
                "UPDATE kb_docs SET category='' WHERE category=? AND tenant_id=?",
                (_name, tenant_id),
            )
            self._conn.execute(
                "DELETE FROM category_grants WHERE category=?", (_name,)
            )
            cur = self._conn.execute(
                "DELETE FROM categories WHERE name=? AND tenant_id=?", (_name, tenant_id)
            )
            self._conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"分类不存在: {_name}")
        return {"deleted": True, "name": _name}

    def list_categories(self, tenant_id: str) -> List[Dict[str, Any]]:
        """列出本租户的显式分类, 附带每分类文档数与授权维度。"""
        with _LOCK:
            rows = self._conn.execute(
                "SELECT c.name, c.description, c.created_at, c.updated_at, "
                "(SELECT COUNT(*) FROM kb_docs d WHERE d.category=c.name AND d.tenant_id=c.tenant_id) AS doc_count "
                "FROM categories c WHERE c.tenant_id=? ORDER BY c.created_at",
                (tenant_id,),
            ).fetchall()
        return [
            {
                "name": r[0], "description": r[1] or "",
                "created_at": r[2], "updated_at": r[3], "doc_count": int(r[4] or 0),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 组×分类 授权矩阵 — P0 分类授权
    # ------------------------------------------------------------------
    def set_category_grant(
        self, group_id: str, category: str, tenant_id: str,
        can_read: bool = False, can_manage: bool = False, by: str = "",
    ) -> Dict[str, Any]:
        """写入某用户组对某分类的授权(读取/管理), 用于权限矩阵。"""
        _cat = (category or "").strip()
        if not group_id or not _cat:
            raise ValueError("用户组与分类不能为空")
        if not self.get_category(_cat, tenant_id):
            raise ValueError(f"分类不存在: {_cat}")
        now = time.time()
        with _LOCK:
            # 更新分类名时同租户可能已改名, 这里按当前分类为准
            self._conn.execute(
                "INSERT INTO category_grants(group_id, category, can_read, can_manage, updated_by, updated_at) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(group_id, category) DO UPDATE SET "
                "can_read=excluded.can_read, can_manage=excluded.can_manage, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (group_id, _cat, 1 if can_read else 0, 1 if can_manage else 0, by, now),
            )
            self._conn.commit()
        return {"group_id": group_id, "category": _cat, "can_read": bool(can_read), "can_manage": bool(can_manage)}

    def batch_set_category_grants(self, group_id: str, categories: Dict[str, Dict[str, Any]], tenant_id: str, by: str = "") -> int:
        """批量写入授权矩阵: categories = {分类名: {"can_read":bool,"can_manage":bool}}。"""
        n = 0
        for cat, flags in (categories or {}).items():
            self.set_category_grant(
                group_id, cat, tenant_id,
                can_read=bool(flags.get("can_read", False)),
                can_manage=bool(flags.get("can_manage", False)),
                by=by,
            )
            n += 1
        return n

    def get_category_grants(self, tenant_id: str) -> List[Dict[str, Any]]:
        """返回本租户分类的完整授权矩阵(含分类是否存在的校验)。"""
        cats = {c["name"] for c in self.list_categories(tenant_id)}
        with _LOCK:
            rows = self._conn.execute(
                "SELECT g.group_id, g.category, g.can_read, g.can_manage, g.updated_by, g.updated_at "
                "FROM category_grants g WHERE g.category IN (%s) " % (",".join("?" for _ in cats) or "''")
                if cats else "SELECT '','',0,0,'',0 WHERE 1=0",
                tuple(cats),
            ).fetchall()
        return [
            {
                "group_id": r[0], "category": r[1],
                "can_read": bool(r[2]), "can_manage": bool(r[3]),
                "updated_by": r[4] or "", "updated_at": r[5],
            }
            for r in rows
        ]

    def grants_for_groups(self, group_ids: List[str]) -> List[Dict[str, Any]]:
        """按组集合汇总授权(用于计算用户的可读/可管分类)。"""
        gids = [g for g in (group_ids or []) if g]
        if not gids:
            return []
        ph = ",".join("?" for _ in gids)
        with _LOCK:
            rows = self._conn.execute(
                "SELECT group_id, category, can_read, can_manage FROM category_grants "
                f"WHERE group_id IN ({ph})",
                gids,
            ).fetchall()
        return [
            {"group_id": r[0], "category": r[1], "can_read": bool(r[2]), "can_manage": bool(r[3])}
            for r in rows
        ]

    def user_readable_categories(self, group_ids: List[str]) -> List[str]:
        """计算一组用户组可读取的分分类名(并集, 去重)。"""
        out: List[str] = []
        seen: set = set()
        for g in self.grants_for_groups(group_ids):
            if g["can_read"] and g["category"] not in seen:
                seen.add(g["category"])
                out.append(g["category"])
        return out

    def user_manageable_categories(self, group_ids: List[str]) -> List[str]:
        """计算一组用户组可管理的分分类名(并集, 去重)。"""
        out: List[str] = []
        seen: set = set()
        for g in self.grants_for_groups(group_ids):
            if g["can_manage"] and g["category"] not in seen:
                seen.add(g["category"])
                out.append(g["category"])
        return out

    def delete_group_grants(self, group_id: str) -> None:
        """删除用户组时级联清理其授权矩阵。"""
        with _LOCK:
            self._conn.execute("DELETE FROM category_grants WHERE group_id=?", (group_id,))
            self._conn.commit()


_store: Optional[DocMetaStore] = None
_store_lock = threading.Lock()


def get_doc_meta_store() -> DocMetaStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = DocMetaStore(get_settings().doc_meta_db_path)
    return _store