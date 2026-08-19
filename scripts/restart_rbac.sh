#!/bin/bash
set -e
cd "$(dirname "$0")/.."

# 管理员口令不硬编码入库, 通过环境变量注入(否则用占位符, 避免泄露真实口令)
ADMIN_USER="${RAG_RBAC_ADMIN_USER:-admin}"
ADMIN_PASS="${RAG_RBAC_ADMIN_PASSWORD:-__REPLACE_ME__}"

# 1. 停旧服务
echo ">>> 停止旧服务..."
PID=$(ss -tlnp 'sport = :8000' 2>/dev/null | grep -oP 'pid=\K[0-9]+' || true)
if [ -n "$PID" ]; then
    kill $PID 2>/dev/null || true
fi
PID=$(fuser 8000/tcp 2>/dev/null | awk '{print $1}' || true)
if [ -n "$PID" ]; then
    kill $PID 2>/dev/null || true
fi
sleep 1

# 2. 创建 .env（启用 RBAC）
echo ">>> 生成 .env..."
JWT_SECRET=$(openssl rand -hex 32 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || echo "dev-secret-change-me-in-production")
cat > .env << ENVEOF
RAG_LLM_PROVIDER=openai
RAG_LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
RAG_LLM_MODEL=glm-4-flash
RAG_LLM_TEMPERATURE=0.1
RAG_EMBEDDING_PROVIDER=fastembed
RAG_FASTEMBED_MODEL=BAAI/bge-small-zh-v1.5
RAG_HF_ENDPOINT=https://hf-mirror.com
RAG_MILVUS_URI=./data/milvus_lite.db
RAG_MILVUS_COLLECTION=manual_chunks
RAG_RETRIEVAL_TOP_K=12
RAG_MANUALS_DIR=./data/manuals
RAG_UPLOADS_DIR=./data/uploads
RAG_AUTO_INGEST_ON_STARTUP=true
RAG_HYBRID_BM25_K=12
RAG_RRF_K=60
RAG_DOC_VERSIONS_DB_PATH=./data/versions.db
RAG_AUTH_MODE=on
RAG_JWT_SECRET=${JWT_SECRET}
RAG_JWT_ALGORITHM=HS256
RAG_JWT_EXPIRE_HOURS=24
RAG_AUTH_DB_PATH=./data/auth.db
RAG_DEFAULT_TENANT=default
RAG_TRACE_DB_PATH=./data/trace.db
RAG_HOST=0.0.0.0
RAG_PORT=8000
ENVEOF
echo "  JWT_SECRET=${JWT_SECRET}"

# 3. 启动服务
echo ">>> 启动服务..."
nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > /tmp/rag-assistant.log 2>&1 &
sleep 3

# 4. 创建管理员
echo ">>> 创建管理员..."
.venv/bin/python scripts/manage_users.py create-admin --username "$ADMIN_USER" --password "$ADMIN_PASS"

# 5. 验证
echo ">>> 验证登录..."
curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}"

echo ""
echo "============================================"
echo "RBAC 已启用!"
echo "管理员: $ADMIN_USER / 口令由 RAG_RBAC_ADMIN_PASSWORD 提供"
echo "JWT 密钥: ${JWT_SECRET}"
echo "============================================"