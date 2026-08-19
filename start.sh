#!/usr/bin/env bash
# 一键启动 RAG 助手服务
# 用法: ./start.sh
# 可选环境变量:
#   RAG_PORT=8000          监听端口
#   RAG_HOST=0.0.0.0       监听地址
set -euo pipefail
cd "$(dirname "$0")"

APP_DIR="$(pwd)"
VENV="$APP_DIR/.venv"
PORT="${RAG_PORT:-8000}"
HOST="${RAG_HOST:-0.0.0.0}"
PID_FILE="$APP_DIR/.run/uvicorn.pid"
LOG_FILE="$APP_DIR/logs/server.log"

# 1. 依赖检查
if [ ! -x "$VENV/bin/uvicorn" ]; then
  echo "[ERROR] 未找到虚拟环境 $VENV"
  echo "        请先执行: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
command -v curl >/dev/null 2>&1 || { echo "[ERROR] 未找到 curl，健康检查将无法执行"; exit 1; }

# 2. 已在运行则直接退出
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "[WARN] 服务已在运行 (PID $(cat "$PID_FILE"))，如需重启请先执行 ./stop.sh"
  exit 0
fi

# 3. 端口占用检查
if command -v ss >/dev/null 2>&1 && ss -lnt 2>/dev/null | grep -q ":$PORT "; then
  echo "[WARN] 端口 $PORT 已被占用，请先释放或通过 RAG_PORT 环境变量修改"
  exit 1
fi

mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

# 4. 加载 .env (如存在，让 .env 中的 RAG_LLM_API_KEY 等配置生效)
if [ -f "$APP_DIR/.env" ]; then
  set -a
  . "$APP_DIR/.env"
  set +a
fi

# 5. 后台启动 uvicorn (setsid 完全脱离控制终端, 防止 Shell 退出时被 SIGHUP)
echo "[INFO] 启动 RAG 助手 ($HOST:$PORT) ..."
setsid nohup "$VENV/bin/uvicorn" app.main:app \
  --host "$HOST" --port "$PORT" --log-level info \
  > "$LOG_FILE" 2>&1 < /dev/null &
PID=$!
echo "$PID" > "$PID_FILE"
disown 2>/dev/null || true

# 6. 轮询健康检查 (最多 60 秒，模型首次加载较慢)
echo "[INFO] 等待服务就绪 ..."
for i in $(seq 1 30); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "[ERROR] 进程已退出，日志末尾:"
    tail -n 20 "$LOG_FILE" 2>/dev/null
    rm -f "$PID_FILE"
    exit 1
  fi
  if curl -s -m 3 "http://127.0.0.1:$PORT/api/v1/health" >/dev/null 2>&1; then
    echo "[OK] 服务已就绪 (PID $PID)"
    echo "      聊天界面: http://localhost:$PORT"
    echo "      API 文档:  http://localhost:$PORT/docs"
    echo "      知识库:    http://localhost:$PORT/documents-browser"
    echo "      日志:      $LOG_FILE"
    echo "      停止:      ./stop.sh"
    exit 0
  fi
  sleep 2
done
echo "[WARN] 60 秒内未就绪，请检查日志: $LOG_FILE"
exit 1
