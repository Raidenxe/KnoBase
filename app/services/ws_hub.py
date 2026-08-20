"""通知实时推送连接池(WebSocket)。

将「已登录用户」与其 /ws/notify 连接做映射, 当 notify_store 写入新通知时,
通过注册到 notify_store 的推手(pusher)跨线程调度到对应连接所在的事件循环,
实现通知实时触达(无需前端轮询)。

设计要点:
- 纯连接管理, 不持有业务逻辑; notify_store 通过 register_pusher 反向回调
- 跨线程安全: 写入通知可能发生在后台线程(启动导入/工单)或异步路由,
  统一用 loop.call_soon_threadsafe 调度 send_json, 避免 asyncio 跨线程问题
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)


class NotifyHub:
    """key = f"{tenant_id}:{user_id}" -> 该用户的所有在线连接"""

    def __init__(self) -> None:
        self._conns: Dict[str, Set[Any]] = {}
        self._loops: Dict[str, Set[asyncio.AbstractEventLoop]] = {}
        self._lock = threading.Lock()
        self._registered = False

    def ensure_pusher_registered(self) -> None:
        """幂等注册推手到 notify_store(延迟 import 避免循环依赖)。"""
        if self._registered:
            return
        from app.services import notify_store

        notify_store.register_pusher(self.push)
        self._registered = True

    def connect(self, ws, tenant_id: str, user_id: str) -> None:
        self.ensure_pusher_registered()
        if not user_id:
            return
        key = f"{tenant_id}:{user_id}"
        with self._lock:
            self._conns.setdefault(key, set()).add(ws)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                self._loops.setdefault(key, set()).add(loop)

    def disconnect(self, ws, tenant_id: str, user_id: str) -> None:
        key = f"{tenant_id}:{user_id}"
        with self._lock:
            wss = self._conns.get(key)
            if wss:
                wss.discard(ws)
                if not wss:
                    self._conns.pop(key, None)
                    self._loops.pop(key, None)

    # ------------------------------------------------------------------
    # 供 notify_store 写入通知后调用的同步推手
    # ------------------------------------------------------------------
    def push(self, notification: Dict[str, Any]) -> None:
        tenant_id = notification.get("tenant_id", "")
        user_id = notification.get("user_id", "")
        key = f"{tenant_id}:{user_id}"
        with self._lock:
            wss: List[Any] = list(self._conns.get(key, set()))
            loops: List[asyncio.AbstractEventLoop] = list(self._loops.get(key, set()))
        if not wss:
            return
        payload = {
            "event": "notification",
            "data": {
                "id": notification.get("id"),
                "type": notification.get("type"),
                "title": notification.get("title", ""),
                "content": notification.get("content", ""),
                "link": notification.get("link", ""),
                "created_at": notification.get("created_at"),
            },
        }
        for loop in loops:
            if loop.is_closed():
                continue
            for ws in wss:
                try:
                    loop.call_soon_threadsafe(self._schedule_send, ws, payload)
                except Exception:  # noqa: BLE001
                    logger.debug("通知推送调度失败(连接可能已关闭)")

    def _schedule_send(self, ws, payload: Dict[str, Any]) -> None:
        asyncio.ensure_future(self._send(ws, payload))

    @staticmethod
    async def _send(ws, payload: Dict[str, Any]) -> None:
        try:
            await ws.send_json(payload)
        except Exception:  # noqa: BLE001
            pass  # 已断开连接由端点 finally 清理


_hub: NotifyHub | None = None
_hub_lock = threading.Lock()


def get_notify_hub() -> NotifyHub:
    global _hub
    if _hub is None:
        with _hub_lock:
            if _hub is None:
                _hub = NotifyHub()
    return _hub