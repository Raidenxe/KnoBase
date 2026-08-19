# 文档版本管理

## 1. 设计

`app/services/versions.py` 用 SQLite（`versions.db`，`RAG_DOC_VERSIONS_DB_PATH`）记录每个文档的修订历史与全文快照：

- `doc_versions(doc_id, doc_name, version, fingerprint, chunk_count, source_file, tenant_id, created_by, created_at)`，主键 `(doc_id, version)`。
- `doc_version_chunks(...)` 保存每次修订的全文快照，用于回滚/查看历史。
- **Milvus 只保留"当前激活版本"的向量**，历史版本以快照形式存于本库，兼顾性能与可回滚。

## 2. 版本规则

- 入库（`ingest_file`）时按 `doc_name+size+mtime` 指纹识别；指纹与上一版本一致则**复用版本号**（增量跳过）。
- 指纹变化 → 版本号 `+1`，写入新快照，Milvus 覆盖更新为当前激活。
- 回滚到某版本 → 用其快照重新向量化覆盖 Milvus，并再 `+1` 生成新修订。

## 3. 接口（挂载于 `/api/v1/documents`）

| 接口 | 说明 |
| --- | --- |
| `GET /{doc_id}/versions` | 修订历史列表（倒序） |
| `GET /{doc_id}/versions/{version}` | 指定版本全文快照 |
| `POST /{doc_id}/versions/{version}/rollback` | 回滚到指定版本并生成新修订 |
| `DELETE /{doc_id}` | 删除文档及其全部版本记录 |

## 4. 示例

```bash
# 历史列表
curl http://host:8000/api/v1/documents/<doc_id>/versions

# 回滚到 v1
curl -X POST http://host:8000/api/v1/documents/<doc_id>/versions/1/rollback
# => { "rolled_back_to": 1, "current_version": 3, "chunk_count": N }
```

## 5. 兼容性

无认证时 `created_by` 为空串；增量跳过逻辑不变（指纹一致的版本不重复生成）。