"""清理测试/临时数据污染脚本。

删除满足测试命名特征的残留用户
- e2e_* / tmp_* 用户名
以及测试会话(traceability 标题)
- "hi" / "新会话" / "测试重命名-回归" 及以"测试"开头的标题

默认仅预览(打印将删除的对象), 加 --apply 才真正删除。
用法:
    python scripts/clean_test_data.py                # 预览
    python scripts/clean_test_data.py --apply        # 执行清理
    python scripts/clean_test_data.py --apply --users-only
    python scripts/clean_test_data.py --apply --conversations-only
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 测试用户命名特征
_USER_PATTERNS = [
    re.compile(r"^e2e_", re.IGNORECASE),
    re.compile(r"^tmp_", re.IGNORECASE),
]
# 测试会话标题特征
_CONV_EXACT = {"hi", "hello", "测试重命名-回归", "新会话", "test", "dicover 测试"}
_CONV_PREFIX = ["测试", "回归", "e2e"]


def _is_test_user(username: str) -> bool:
    return any(p.match(username or "") for p in _USER_PATTERNS)


def _is_test_conv(title: str) -> bool:
    t = (title or "").strip().lower()
    if t in _CONV_EXACT:
        return True
    return any(t.startswith(p) for p in _CONV_PREFIX)


def main() -> None:
    parser = argparse.ArgumentParser(description="清理测试/临时数据污染")
    parser.add_argument("--apply", action="store_true", help="真正执行删除(缺省仅预览)")
    parser.add_argument("--users-only", action="store_true", help="仅清理测试用户")
    parser.add_argument("--conversations-only", action="store_true", help="仅清理测试会话")
    args = parser.parse_args()

    from app.services.auth_store import get_auth_store
    from app.services.history import get_conversation_store

    store = get_auth_store()
    do_delete = args.apply
    verb = "将删除" if not do_delete else "已删除"

    # 1) 测试用户
    if not args.conversations_only:
        users = store.list_users()
        targets = [u for u in users if _is_test_user(u["username"])]
        print(f"\n== 测试用户命中 {len(targets)} 条 ==")
        for u in targets:
            print(f"  - {u['username']:<24} role={u['role']:<8} tenant={u['tenant_id']}")
        for u in targets:
            if do_delete:
                store.delete_user(u["id"])
        print(f"用户 {verb} {len(targets)} 条")

    # 2) 测试会话
    if not args.users_only:
        convs = get_conversation_store().list_conversations(limit=10000)
        targets = [c for c in convs if _is_test_conv(c.get("title", ""))]
        print(f"\n== 测试会话命中 {len(targets)} 条 ==")
        for c in targets:
            print(f"  - {c.get('id')}  「{c.get('title')}」")
        for c in targets:
            if do_delete:
                get_conversation_store().delete_conversation(c["id"])
        print(f"会话 {verb} {len(targets)} 条")

    if not do_delete:
        print("\n(仅预览, 未实际删除; 确认无误后加 --apply 执行)")


if __name__ == "__main__":
    main()