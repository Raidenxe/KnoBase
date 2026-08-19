# 多租户与权限（RBAC）

## 1. 概述

通过 `RAG_AUTH_MODE` 开关控制：

- `off`（默认，演示模式）：无认证，所有接口匿名访问，隐式归属 `default` 租户，行为与旧版本完全一致。
- `on`（生产模式）：启用 JWT Bearer 认证、角色权限与租户级数据隔离。

## 2. 角色

| 角色 | 能力 |
| --- | --- |
| `admin` | 跨租户系统管理（可操作任意租户资源） |
| `owner` | 租户内全权（读写 + 管理） |
| `member` | 租户内读写 |
| `viewer` | 租户内只读 |

写操作（文档导入/删除、会话删除、回滚等）需 `write_required`（`owner/member/admin`）；
读操作需 `read_required`；评估摘要等系统接口需 `admin_required`。

> 认证关闭时，各类守卫自动放行，保证演示与既有测试零改动。

## 3. 租户隔离

数据模型在底层统一附加 `tenant_id` 字段：

- **Milvus chunk** 增加 `tenant_id` 标量字段，检索/删除/清单均带 `tenant_id` 过滤表达式。
- **对话/会话**（`history.db`）增加 `tenant_id` 列，列表/详情/删除按租户过滤。
- **文档版本**（`versions.db`）记录 `tenant_id`。

## 4. 使用

```bash
# 登录获取 token
curl -X POST http://host:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"secret"}'
# 返回 { "access_token": "...", "user": {...} }

# 携带 token 访问受保护接口
curl http://host:8000/api/v1/documents \
  -H 'Authorization: Bearer <token>'
```

WebSocket：`wss://host/api/v1/ws/chat?token=<token>`（auth on 时必填）。

## 5. 安全要点

- 口令以 `pbkdf2_hmac(sha256, 200k)` 哈希存储，不落明文。
- `viewer` 与跨租户越权会被 403 拦截；响应尚未暴露其他租户数据。
- 生产务必配置 `RAG_JWT_SECRET`，建议 32+ 字节随机串，勿提交版本库。