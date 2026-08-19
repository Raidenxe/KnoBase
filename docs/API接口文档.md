# API 接口文档
## KnoBase RAG智能助手

| 元数据 | 内容 |
|---|---|
| 文档编号 | DOC-API-002 |
| 密级 | 内部 |
| 版本 | v2.0 |
| 适用范围 | 后端研发、联调、三方接入 |
| 维护人 | 平台研发团队 |
| 上次更新 | 2026-08-19 |

**修订历史**

| 版本 | 日期 | 修订人 | 变更摘要 |
|---|---|---|---|
| v2.0 | 2026-08-19 | 平台研发 | 全量盘点 /api/v1 端点并细化鉴权守卫 |

> 通用约定：Base URL `http://<host>:<port>`；非登录接口 `Authorization: Bearer <JWT>`；SSE 事件用 `event:`/`data:` 分隔。即时交互式定义见 `/docs`（Swagger）与 `/openapi.json`。

**鉴权守卫速查**

| 守卫 | 含义 |
|---|---|
| get_current_user | 任何已登录用户（认证关时匿名） |
| read_required | 有可读权限（viewer+） |
| write_required | 可写（member+）；认证关时放行 |
| admin_required | 仅 admin |
| owner_required | owner/admin（跨租户创建需 admin） |

---

## 1. 认证 `POST /api/v1/auth/*`

| 方法 | 路径 | 守卫 | 说明 |
|---|---|---|---|
| POST | /auth/login | 无 | 登录，返回 JWT 与用户信息 |
| GET | /auth/me | current_user | 当前用户 |
| GET | /auth/tenants | admin | 租户列表 |
| POST | /auth/users | owner | 创建用户 |
| GET | /auth/users | owner | 用户列表 |
| GET | /auth/users/{id} | owner | 用户详情 |
| PUT | /auth/users/{id} | owner | 编辑用户（角色等） |
| POST | /auth/users/{id}/disable · /enable | owner | 启用/禁用 |
| POST | /auth/users/{id}/reset-password | owner | 重置密码 |
| DELETE | /auth/users/{id} | owner | 删除用户 |
| GET/PUT | /auth/profile | current_user | 个人资料 |
| POST | /auth/profile/change-password | current_user | 修改密码 |
| GET | /auth/profile/login-logs | current_user | 我的登录日志 |
| GET/POST | /auth/groups | owner | 用户组列表/新增 |
| PUT/DELETE | /auth/groups/{gid} | owner / admin(删除) | 编辑/删除组 |
| GET/PUT | /auth/groups/{gid}/members | owner | 组成员查询/整体设置 |
| POST/DELETE | /auth/groups/{gid}/members[/{uid}] | owner | 增/删组员 |

## 2. 分类授权 `…/api/v1/categories*`

| 方法 | 路径 | 守卫 | 说明 |
|---|---|---|---|
| GET | /categories | current_user | 知识库分类列表 |
| GET | /categories/grants/matrix | current_user | 授权矩阵视图 |
| PUT | /categories/grants | owner | 设某组对某分类授权 |
| PUT | /categories/grants/batch | owner | 批量授权 |
| POST | /categories | owner | 新增分类 |
| PUT/DELETE | /categories/{category} | owner | 编辑/删除分类 |

## 3. 对话 `…/api/v1/conversations`

| 方法 | 路径 | 守卫 | 说明 |
|---|---|---|---|
| GET | /conversations | current_user | 会话列表 |
| GET | /conversations/{id} | current_user | 会话详情（含消息） |
| PATCH | /conversations/{id} | write | 重命名 |
| DELETE | /conversations/{id} | write | 删除会话 |
| GET | /conversations/{id}/export?format=markdown\|text | read | 导出 |
| POST | /conversations/{id}/export/share | write | 生成分享（可带 ttl_seconds） |
| DELETE | /conversations/share/{token} | write | 撤销分享 |
| GET | /conversations/admin/stats | admin | 全量对话统计 |
| GET | /shares/{token} | 无 | 匿名查看分享 |

## 4. 问答与反馈

| 方法 | 路径 | 守卫 | 说明 |
|---|---|---|---|
| POST | /chat | current_user | 提问（非流式） |
| POST | /chat/stream | current_user | 提问（SSE 流式，含 citations/suggestions） |
| WS | /ws/chat | ws 鉴权 | WebSocket 实时问答 |
| POST | /feedback | write | 提交/更新反馈 |
| GET | /feedback/stats | write | 反馈统计 |

## 5. 文档与知识库 `…/api/v1/documents`

| 方法 | 路径 | 守卫 | 说明 |
|---|---|---|---|
| GET | /documents | read | 文档清单（搜索/状态/分类/分页） |
| POST | /documents/import | write | 按路径批量导入 |
| POST | /documents/upload | write | 上传文件导入 |
| POST | /documents/scan | write | 目录扫描导入（?background=true 异步） |
| POST | /documents/web-import | write | 网页抓取导入 |
| POST | /documents/batch-delete · /batch-meta | write | 批量删除/设元数据 |
| GET | /documents/stats | read | 每文档命中/覆盖统计 |
| GET | /documents/categories | read | 分类清单 |
| GET/DELETE | /documents/{id} · /{id}/detail | read/write | 查询/详情/删除 |
| PUT | /documents/{id}/meta · /{id}/profile | write | 设分类权限/编辑展示名版本 |
| POST | /documents/{id}/replace · /retry | write | 覆盖上传/失败重试 |
| GET | /documents/{id}/content | read | 文档全文（知识库浏览器） |
| GET | /documents/{id}/versions · /versions/{v} | read | 修订历史/指定版本快照 |
| POST | /documents/{id}/versions/{v}/rollback | write | 回滚到指定版本 |
| PUT/POST | /documents/categories/{category} · /categories/delete | write | 分类改名/删除 |

## 6. 后台任务

| 方法 | 路径 | 守卫 | 说明 |
|---|---|---|---|
| GET | /tasks | 登录 | 任务列表 |
| GET | /tasks/{id} | 登录 | 任务进度/状态 |

## 7. 可观测与运维 `…/api/v1/*`

| 方法 | 路径 | 守卫 | 说明 |
|---|---|---|---|
| GET | /traces · /traces/{id} · /traces/{id}/detail | current_user | 链路列表/单次/耗时拆分 |
| GET | /metrics/basic | current_user | 分阶段 P50/P95/拒答/失败率 |
| GET | /eval/summary | admin | 最近评估摘要 |
| GET | /metrics/endpoints · /metrics/usage | admin | 接口 P99/QPS、LLM 用量 |
| GET/PUT | /admin/prompt | admin | 提示词查询/热更新 |
| GET/PUT | /admin/models · /llm · /embedding | admin | 模型配置查看/切换 |
| GET/PUT | /admin/retrieval | admin | 检索参数查看/调整 |
| GET | /admin/resource/current · /series | admin | 资源水位/曲线 |
| GET | /admin/slow-requests | admin | 慢请求明细 |
| GET | /audit | admin | 敏感操作审计 |

## 8. 公共/健康/工单通知

| 方法 | 路径 | 守卫 | 说明 |
|---|---|---|---|
| GET | /health | 无 | 健康检查（milvus/embedding/llm/chunks） |
| GET | /documents-browser | 无(页面) | 知识库浏览器 |
| — | /notifications* · /tickets* | 登录/工单相关 | 通知、工单（详见专项文档） |
| GET | /eval/summary | admin | 评估摘要 |

## 9. 常见响应结构

```jsonc
// SSE done 事件 (POST /chat/stream)
{"conversation_id":"..","answer":"..","citations":[{"index":1,"doc_id":"..",
 "chunk_id":"..","doc_name":"..","section_path":"..","page":-1,"score":0.03,"snippet":".."}],
 "verified":true,"message_id":62,"suggestions":[".."],"metrics":{"retrieval_ms":251,
 "generation_ms":559,"total_ms":837}}
```

## 10. 安全提示

- 管理端接口（admin/owner）后端强鉴权，前端仅作入口显隐。
- 会话数据按租户隔离；生产多用户场景建议启用按用户隔离（见缺陷清单）。
- 涉及文档全文的读接口应用于非授权文档时，应经分类/私有校验（当前 `/content`、`/detail`、`/versions` 需复核，见改进建议）。