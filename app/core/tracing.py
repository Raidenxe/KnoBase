"""全链路追踪: 全局请求级 trace_id 注入。

- 每位请求生成 trace_id(优先透传 X-Request-Id), 写回 X-Request-Id 响应头
- trace_id 通过 state 注入对话图节点, 供 TraceStore 记录 span
- 仅提供轻量中间件, 不阻塞关键路径(bare ASGI wrapper)
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class TracingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        provided = None
        for k, v in scope.get("headers") or ():
            if k == b"x-request-id":
                try:
                    provided = v.decode("ascii").strip()
                except Exception:  # noqa: BLE001
                    provided = None
                break
        trace_id = (provided[:64] if provided else "") or uuid.uuid4().hex[:16]
        scope.setdefault("state", {})["trace_id"] = trace_id

        started = time.perf_counter()
        started_wall = time.time()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                if all(k != b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", trace_id.encode()))
                    message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            ms = int((time.perf_counter() - started) * 1000)
            if scope["type"] == "http":
                # 接口性能埋点: 仅统计 /api/* 业务接口, 排除静态资源噪声
                path = (scope.get("path") or "").split("?", 1)[0]
                if path.startswith("/api/"):
                    from app.services.metrics import get_endpoint_metrics
                    get_endpoint_metrics().record(path, ms)
                if ms > 500:
                    logger.info("[trace] %s 全程 %d ms", trace_id, ms)
                if path.startswith("/api/"):
                    # 慢请求明细: 全程超阈值的 /api/* 请求记录 (供 management 面板)
                    from app.services.slow_requests import get_slow_request_store
                    get_slow_request_store().record(
                        trace_id, path, ms,
                        started_at=started_wall,
                    )