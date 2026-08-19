#!/usr/bin/env bash
# 一键停止 RAG 助手服务
# 用法: ./stop.sh
# 可选环境变量:
#   RAG_PORT=8000          监听端口(兜底按端口清理孤儿进程)
set -uo pipefail
cd "$(dirname "$0")"

APP_DIR="$(pwd)"
PID_FILE="$APP_DIR/.run/uvicorn.pid"
PORT="${RAG_PORT:-8000}"
stopped=0

# 1. 通过 PID 文件优雅关闭
if [ -f "$PID_FILE" ]; then
  PID="$(cat "$PID_FILE")"
  if kill -0 "$PID" 2>/dev/null; then
    echo "[INFO] 停止服务 (PID $PID) ..."
    kill "$PID" 2>/dev/null || true
    # 优雅等待最多 10 秒
    for _ in $(seq 1 20); do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.5
    done
    # 仍未退出则强制
    if kill -0 "$PID" 2>/dev/null; then
      echo "[WARN] 优雅关闭超时，强制终止 (SIGKILL)"
      kill -9 "$PID" 2>/dev/null || true
      sleep 1
    fi
    stopped=1
  fi
  rm -f "$PID_FILE"
fi

# 2. 兜底: 按端口清理残留/孤儿进程
if command -v fuser >/dev/null 2>&1; then
  PORT_PIDS=$(fuser -n tcp "$PORT" 2>/dev/null | tr -d ' \t' || true)
  if [ -n "$PORT_PIDS" ]; then
    echo "[INFO] 通过端口 $PORT 清理残留进程: $PORT_PIDS"
    kill -9 $PORT_PIDS 2>/dev/null || true
    stopped=1
  fi
elif command -v ss >/dev/null 2>&1; then
  PORT_PIDS=$(ss -lntp 2>/dev/null | grep ":$PORT " | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u | tr '\n' ' ')
  if [ -n "$PORT_PIDS" ]; then
    echo "[INFO] 通过端口 $PORT 清理残留进程: $PORT_PIDS"
    kill -9 $PORT_PIDS 2>/dev/null || true
    stopped=1
  fi
fi

if [ "$stopped" = "1" ]; then
  echo "[OK] 已停止"
else
  echo "[INFO] 未发现运行中的服务"
fi
exit 0
