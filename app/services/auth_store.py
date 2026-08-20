"""认证与用户/租户存储(SQLite auth.db)。

- 用户: username / pbkdf2 口令哈希 / role / tenant_id
- 租户: 独立知识库与数据隔离的单位
- 角色: admin(系统管理) | owner(租户内全权) | member(租户内读写) | viewer(租户内只读)

口令哈希使用标准库 hashlib.pbkdf2_hmac(sha256, 200k 迭代), 不依赖额外 C 扩展。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.config import get_settings

_LOCK = threading.RLock()

ROLES = {"admin", "owner", "member", "viewer"}
# 各角色允许的操作能力
WRITE_ROLES = {"admin", "owner", "member"}
READ_ROLES = {"admin", "owner", "member", "viewer"}

_PBKDF2_ITER = 200_000

# 登录节流策略: 连续错误达到上限后锁定
LOGIN_MAX_ATTEMPTS = 5
ACCOUNT_LOCK_SECONDS = 300
# 口令强度
PASSWORD_MIN_LEN = 8


def validate_password(password: str) -> Optional[str]:
    """校验口令强度; 通过返回 None, 否则返回原因。"""
    if not password:
        return "密码不能为空"
    if len(password) < PASSWORD_MIN_LEN:
        return f"密码长度至少 {PASSWORD_MIN_LEN} 位"
    has_upper = any(c.isupper() for c in password)
    has_lower = any(c.islower() for c in password)
    has_digit = any(c.isdigit() for c in password)
    if not (has_upper and has_lower and has_digit):
        return "密码需同时包含大小写字母与数字"
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ITER)
    return f"pbkdf2${_PBKDF2_ITER}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt, expected = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), expected)
    except Exception:  # noqa: BLE001
        return False


class AuthStore:
    def __init__(self, db_path: str) -> None:
        from pathlib import Path

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenants(
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    tenant_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member',
                    is_active INTEGER DEFAULT 1,
                    must_change_password INTEGER DEFAULT 0,
                    last_login_at INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_groups(
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT DEFAULT '',
                    created_by TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_group_members(
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    PRIMARY KEY(group_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS login_logs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    ip TEXT DEFAULT '',
                    device TEXT DEFAULT '',
                    ts INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_login_logs_user ON login_logs(user_id, ts);
                CREATE TABLE IF NOT EXISTS login_attempts(
                    username TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            self._migrate_users()
            self._conn.commit()
            # 确保默认租户存在(auth on 时兜底)
            if not self.get_tenant(get_settings().default_tenant):
                self.create_tenant(get_settings().default_tenant, "默认租户")
            self._seed_default_groups()

    def _migrate_users(self) -> None:
        """为旧 auth.db 补充新增列(email / must_change_password / last_login_at)。"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(users)").fetchall()}
        for name, ddl in (
            ("email", "ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''"),
            ("must_change_password", "ALTER TABLE users ADD COLUMN must_change_password INTEGER DEFAULT 0"),
            ("last_login_at", "ALTER TABLE users ADD COLUMN last_login_at INTEGER DEFAULT 0"),
        ):
            if name not in cols:
                self._conn.execute(ddl)

    def _seed_default_groups(self) -> None:
        """初始化内置默认用户组(幂等)。"""
        defaults = [
            ("admin", "管理员：全部权限"),
            ("ops", "运维人员：读写大部分分类"),
            ("viewer", "只读访客：仅部分分类可读"),
        ]
        for gid, desc in defaults:
            if self.get_group_by_name(gid):
                continue
            self.create_group(gid, desc)

    # ------------------------------------------------------------------
    # 租户
    # ------------------------------------------------------------------
    def create_tenant(self, tenant_id: str = "", name: str = "") -> Dict[str, Any]:
        from datetime import datetime, timezone

        tid = tenant_id or uuid.uuid4().hex[:10]
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _LOCK:
            self._conn.execute(
                "INSERT OR IGNORE INTO tenants(id,name,created_at) VALUES(?,?,?)",
                (tid, name or tid, ts),
            )
            self._conn.commit()
        return {"id": tid, "name": name or tid, "created_at": ts}

    def get_tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        with _LOCK:
            row = self._conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        return dict(row) if row else None

    def list_tenants(self) -> List[Dict[str, Any]]:
        with _LOCK:
            rows = self._conn.execute("SELECT * FROM tenants ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 用户
    # ------------------------------------------------------------------
    def create_user(
        self, username: str, password: str, tenant_id: str = "",
        role: str = "member", display_name: str = "",
        email: str = "",
    ) -> Dict[str, Any]:
        if role not in ROLES:
            raise ValueError(f"非法角色 {role}, 可选: {sorted(ROLES)}")
        uid = uuid.uuid4().hex[:16]
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _LOCK:
            try:
                self._conn.execute(
                    "INSERT INTO users(id,username,password_hash,display_name,email,"
                    " tenant_id,role,is_active,must_change_password,last_login_at,created_at) "
                    "VALUES(?,?,?,?,?,?,?,1,1,0,?)",
                    (uid, username, hash_password(password), display_name, (email or "").strip(),
                     tenant_id or get_settings().default_tenant, role, ts),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"用户名已存在: {username}") from exc
        return self.get_user(uid)  # type: ignore[return-value]

    def update_profile(self, user_id: str, display_name: Optional[str] = None,
                       email: Optional[str] = None) -> Dict[str, Any]:
        with _LOCK:
            if display_name is not None:
                self._conn.execute("UPDATE users SET display_name=? WHERE id=?", (display_name, user_id))
            if email is not None:
                self._conn.execute("UPDATE users SET email=? WHERE id=?", ((email or "").strip(), user_id))
            self._conn.commit()
        return self.get_user(user_id) or {}

    def set_active(self, user_id: str, is_active: bool) -> bool:
        with _LOCK:
            cur = self._conn.execute(
                "UPDATE users SET is_active=? WHERE id=?", (1 if is_active else 0, user_id)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def update_role(self, user_id: str, role: str) -> bool:
        if role not in ROLES:
            raise ValueError(f"非法角色 {role}")
        with _LOCK:
            cur = self._conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
            self._conn.commit()
        return cur.rowcount > 0

    def reset_password(self, user_id: str, new_password: str, force_change: bool = True) -> bool:
        reason = validate_password(new_password)
        if reason:
            raise ValueError(reason)
        with _LOCK:
            cur = self._conn.execute(
                "UPDATE users SET password_hash=?, must_change_password=? WHERE id=?",
                (hash_password(new_password), 1 if force_change else 0, user_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def change_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        user = self.get_user(user_id)
        if not user or not verify_password(old_password, user.get("password_hash", "")):
            return False
        if old_password == new_password:
            return False
        reason = validate_password(new_password)
        if reason:
            return False  # 弱口令拒绝修改(路由层给出更具体提示可另行处理)
        with _LOCK:
            self._conn.execute(
                "UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                (hash_password(new_password), user_id),
            )
            self._conn.commit()
        return True

    def record_login(self, user_id: str, ip: str = "", device: str = "") -> None:
        """记录一次登录: 更新用户最近登录时间, 并写入登录日志(供用户自查/安全审计)。"""
        now = int(time.time())
        with _LOCK:
            self._conn.execute(
                "UPDATE users SET last_login_at=? WHERE id=?", (now, user_id)
            )
            self._conn.execute(
                "INSERT INTO login_logs(user_id, ip, device, ts) VALUES(?,?,?,?)",
                (user_id, (ip or "")[:64], (device or "")[:200], now),
            )
            self._conn.commit()

    def list_login_logs(self, user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """返回某用户的登录日志(新→旧), 含 IP / 时间 / 设备。"""
        limit = max(1, min(int(limit or 20), 100))
        with _LOCK:
            rows = self._conn.execute(
                "SELECT ip, device, ts FROM login_logs "
                "WHERE user_id=? ORDER BY ts DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        out = []
        for r in rows:
            ts = int(r[2])
            out.append({
                "ip": r[0] or "", "device": r[1] or "",
                "ts": ts, "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            })
        return out

    # ------------------------------------------------------------------
    # 用户组
    # ------------------------------------------------------------------
    def create_group(self, name: str, description: str = "", by: str = "") -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("用户组名不能为空")
        gid = uuid.uuid4().hex[:12]
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _LOCK:
            try:
                self._conn.execute(
                    "INSERT INTO user_groups(id,name,description,created_by,created_at) VALUES(?,?,?,?,?)",
                    (gid, name, description or "", by, ts),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"用户组已存在: {name}") from exc
        return self.get_group(gid) or {}

    def get_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        with _LOCK:
            row = self._conn.execute("SELECT * FROM user_groups WHERE id=?", (group_id,)).fetchone()
        return dict(row) if row else None

    def get_group_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with _LOCK:
            row = self._conn.execute("SELECT * FROM user_groups WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def list_groups(self) -> List[Dict[str, Any]]:
        with _LOCK:
            rows = self._conn.execute("SELECT * FROM user_groups ORDER BY created_at").fetchall()
        result = []
        for g in rows:
            d = dict(g)
            d["member_count"] = self._group_member_count(d["id"])
            result.append(d)
        return result

    def update_group(self, group_id: str, name: Optional[str] = None,
                     description: Optional[str] = None) -> Dict[str, Any]:
        cur = self.get_group(group_id)
        if not cur:
            raise ValueError("用户组不存在")
        new_name = (name if name is not None else cur["name"]).strip()
        new_desc = description if description is not None else cur["description"]
        if not new_name:
            raise ValueError("用户组名不能为空")
        with _LOCK:
            try:
                self._conn.execute(
                    "UPDATE user_groups SET name=?, description=? WHERE id=?",
                    (new_name, new_desc, group_id),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"用户组已存在: {new_name}") from exc
        return self.get_group(group_id) or {}

    def delete_group(self, group_id: str) -> Dict[str, Any]:
        """删除用户组; 组内仍有成员时抛错并保留; 级联清理分类授权矩阵。"""
        if self._group_member_count(group_id) > 0:
            raise ValueError("组内仍有用户, 请先将用户移出该组")
        with _LOCK:
            self._conn.execute("DELETE FROM user_group_members WHERE group_id=?", (group_id,))
            self._conn.execute("DELETE FROM user_groups WHERE id=?", (group_id,))
            self._conn.commit()
        from app.services.doc_meta import get_doc_meta_store
        try:
            get_doc_meta_store().delete_group_grants(group_id)
        except Exception:  # noqa: BLE001
            pass
        return {"deleted": True}

    def _group_member_count(self, group_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM user_group_members WHERE group_id=?", (group_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def add_group_member(self, group_id: str, user_id: str) -> bool:
        if not self.get_group(group_id) or not self.get_user(user_id):
            raise ValueError("用户组或用户不存在")
        with _LOCK:
            self._conn.execute(
                "INSERT OR IGNORE INTO user_group_members(group_id,user_id) VALUES(?,?)",
                (group_id, user_id),
            )
            self._conn.commit()
        return True

    def remove_group_member(self, group_id: str, user_id: str) -> bool:
        with _LOCK:
            cur = self._conn.execute(
                "DELETE FROM user_group_members WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def group_members(self, group_id: str) -> List[Dict[str, Any]]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT u.id, u.username, u.display_name, u.email, u.role, u.is_active "
                "FROM user_group_members m JOIN users u ON u.id=m.user_id "
                "WHERE m.group_id=? ORDER BY u.created_at", (group_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def user_groups(self, user_id: str) -> List[Dict[str, Any]]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT g.* FROM user_group_members m JOIN user_groups g ON g.id=m.group_id "
                "WHERE m.user_id=? ORDER BY g.created_at", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def replace_user_groups(self, user_id: str, group_ids: List[str]) -> None:
        """全量替换用户的组归属(先清空再写入)。"""
        gids = [g for g in (group_ids or []) if g]
        with _LOCK:
            self._conn.execute("DELETE FROM user_group_members WHERE user_id=?", (user_id,))
            for gid in gids:
                if self._conn.execute(
                    "SELECT 1 FROM user_groups WHERE id=?", (gid,)
                ).fetchone():
                    self._conn.execute(
                        "INSERT OR IGNORE INTO user_group_members(group_id,user_id) VALUES(?,?)",
                        (gid, user_id),
                    )
            self._conn.commit()

    def replace_group_members(self, group_id: str, user_ids: List[str]) -> None:
        """全量替换组成员。"""
        uids = [u for u in (user_ids or []) if u]
        with _LOCK:
            self._conn.execute("DELETE FROM user_group_members WHERE group_id=?", (group_id,))
            for uid in uids:
                if self._conn.execute(
                    "SELECT 1 FROM users WHERE id=?", (uid,)
                ).fetchone():
                    self._conn.execute(
                        "INSERT OR IGNORE INTO user_group_members(group_id,user_id) VALUES(?,?)",
                        (group_id, uid),
                    )
            self._conn.commit()

    def delete_user(self, user_id: str) -> None:
        """删除用户: 清理组归属; 保留其创建的文档记录(避免孤儿数据)。"""
        with _LOCK:
            self._conn.execute("DELETE FROM user_group_members WHERE user_id=?", (user_id,))
            self._conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            self._conn.commit()

    def users_with_group_names(self, users: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """为用户列表补充所属用户组名(聚合查询), 便于管理界面展示。"""
        if not users:
            return []
        ids = [u["id"] for u in users]
        ph = ",".join("?" for _ in ids)
        with _LOCK:
            rows = self._conn.execute(
                "SELECT m.user_id, g.name FROM user_group_members m "
                "JOIN user_groups g ON g.id=m.group_id WHERE m.user_id IN (" + ph + ") "
                "ORDER BY m.user_id, g.created_at",
                ids,
            ).fetchall()
        by_user: Dict[str, List[str]] = {}
        for r in rows:
            by_user.setdefault(r[0], []).append(r[1])
        out = []
        for u in users:
            d = dict(u)
            d["groups"] = by_user.get(d["id"], [])
            out.append(d)
        return out

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        with _LOCK:
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username=?", (username,)
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # 登录节流 / 账号锁定
    # ------------------------------------------------------------------
    def login_status(self, username: str) -> Dict[str, Any]:
        """返回该用户名当前的锁定状态: {'locked':bool, 'retry_in':秒}。"""
        now = int(time.time())
        with _LOCK:
            row = self._conn.execute(
                "SELECT attempts, locked_until FROM login_attempts WHERE username=?",
                (username,),
            ).fetchone()
        attempts = int(row[0]) if row else 0
        locked_until = int(row[1]) if row else 0
        if locked_until > now:
            return {"locked": True, "retry_in": locked_until - now, "attempts": attempts}
        # 锁定已过期则清理, 重新计数
        if locked_until and row:
            with _LOCK:
                self._conn.execute("DELETE FROM login_attempts WHERE username=?", (username,))
                self._conn.commit()
        return {"locked": False, "retry_in": 0, "attempts": attempts}

    def register_login_failure(self, username: str) -> int:
        """记录一次登录失败; 若累计达上限则锁定账号, 返回当前失败次数。"""
        now = int(time.time())
        with _LOCK:
            row = self._conn.execute(
                "SELECT attempts FROM login_attempts WHERE username=?", (username,)
            ).fetchone()
            attempts = (int(row[0]) if row else 0) + 1
            locked_until = now + ACCOUNT_LOCK_SECONDS if attempts >= LOGIN_MAX_ATTEMPTS else 0
            self._conn.execute(
                "INSERT INTO login_attempts(username, attempts, locked_until, updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(username) DO UPDATE SET "
                "attempts=excluded.attempts, locked_until=excluded.locked_until, "
                "updated_at=excluded.updated_at",
                (username, attempts, locked_until, now),
            )
            self._conn.commit()
        return attempts

    def register_login_success(self, username: str) -> None:
        """登录成功时清除该用户的失败计数。"""
        with _LOCK:
            self._conn.execute("DELETE FROM login_attempts WHERE username=?", (username,))
            self._conn.commit()

    # ------------------------------------------------------------------
    def authenticate(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        user = self.get_user_by_username(username)
        if not user or not user["is_active"]:
            return None
        if not verify_password(password, user["password_hash"]):
            return None
        return user

    def list_users(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM users"
        args: tuple = ()
        if tenant_id:
            sql += " WHERE tenant_id=?"
            args = (tenant_id,)
        sql += " ORDER BY created_at"
        with _LOCK:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def update_role(self, user_id: str, role: str) -> bool:
        if role not in ROLES:
            raise ValueError(f"非法角色 {role}")
        with _LOCK:
            cur = self._conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
            self._conn.commit()
        return cur.rowcount > 0


_store: Optional[AuthStore] = None


def get_auth_store() -> AuthStore:
    global _store
    if _store is None:
        _store = AuthStore(get_settings().auth_db_path)
    return _store