"""用户管理 CLI: 创建管理员、列出用户、重置密码等操作。

用法:
    python scripts/manage_users.py create-admin --username admin --password <密码> [--tenant <租户名>]
    python scripts/manage_users.py create-user --username <用户名> --password <密码> --role <角色> [--tenant <租户名>]
    python scripts/manage_users.py list-users
    python scripts/manage_users.py reset-password --username <用户名> --password <新密码>
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.auth_store import get_auth_store


def cmd_create_admin(args: argparse.Namespace) -> None:
    store = get_auth_store()
    tenant_id = args.tenant or "default"
    store.create_tenant(tenant_id)
    user = store.create_user(
        username=args.username,
        password=args.password,
        tenant_id=tenant_id,
        role="admin",
        display_name=args.username,
    )
    print(f"管理员已创建: id={user['id']} username={user['username']} tenant={tenant_id}")


def cmd_create_user(args: argparse.Namespace) -> None:
    store = get_auth_store()
    tenant_id = args.tenant or "default"
    store.create_tenant(tenant_id)
    user = store.create_user(
        username=args.username,
        password=args.password,
        tenant_id=tenant_id,
        role=args.role,
        display_name=args.username,
    )
    print(f"用户已创建: id={user['id']} username={user['username']} role={args.role} tenant={tenant_id}")


def cmd_list_users(args: argparse.Namespace) -> None:
    store = get_auth_store()
    users = store.list_users()
    if not users:
        print("(无用户)")
        return
    print(f"{'用户名':<16} {'角色':<10} {'租户':<12} {'状态':<6}")
    print("-" * 48)
    for u in users:
        print(f"{u['username']:<16} {u['role']:<10} {u['tenant_id']:<12} {'active' if u.get('is_active') else 'disabled':<6}")


def cmd_reset_password(args: argparse.Namespace) -> None:
    store = get_auth_store()
    user = store.get_user_by_username(args.username)
    if not user:
        print(f"用户不存在: {args.username}", file=sys.stderr)
        sys.exit(1)
    store.reset_password(user["id"], args.password, force_change=True)  # 强制下次登录修改
    print(f"密码已重置(强制下次登录修改): {args.username}")


def main() -> None:
    parser = argparse.ArgumentParser(description="RBAC 用户管理工具")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("create-admin", help="创建管理员")
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--tenant", default="default")

    p = sub.add_parser("create-user", help="创建普通用户")
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--role", default="member", choices=["owner", "member", "viewer"])
    p.add_argument("--tenant", default="default")

    p = sub.add_parser("list-users", help="列出所有用户")

    p = sub.add_parser("reset-password", help="重置密码")
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)

    args = parser.parse_args()
    if args.command == "create-admin":
        cmd_create_admin(args)
    elif args.command == "create-user":
        cmd_create_user(args)
    elif args.command == "list-users":
        cmd_list_users(args)
    elif args.command == "reset-password":
        cmd_reset_password(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()