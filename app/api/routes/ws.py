"""WebSocket 实时问答: 复用 chat_service.chat_stream, 逐事件推送 JSON 帧。

客户端协议:
    > {"question": "...", "conversation_id": "?", "token": "?"}  首次消息
    < {"event": "status", "data": {...}}
    < {"event": "token",  "data": {"content": "..."}}
    < {"event": "done",   "data": {全文/引用/指标}}
    < {"event": "error",  "data": {"detail": "..."}}
    断开前可继续发送 {"question": "...", "conversation_id": "..."} 进行多轮。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.core.security import current_tenant_id, get_ws_user
from app.services.chat_service import get_chat_service

router = APIRouter(tags=["chat-ws"])

_MAX_MSG = 2000


async def _send(ws: WebSocket, event: str, data: dict) -> None:
    try:
        await ws.send_json({"event": event, "data": data})
    except Exception:  # noqa: BLE001
        raise WebSocketDisconnect(code=1000)


@router.websocket("/api/v1/ws/chat")
async def ws_chat(ws: WebSocket) -> None:
    await ws.accept()
    user = await get_ws_user(ws)           # auth off 时返回匿名用户, 不强制鉴权
    tenant_id = current_tenant_id(user)
    conversation_id: str | None = None
    service = get_chat_service()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send(ws, "error", {"detail": "消息必须是 JSON 对象"})
                continue
            question = (msg.get("question") or "").strip()
            if not question:
                await _send(ws, "error", {"detail": "question 不能为空"})
                continue
            question = question[:_MAX_MSG]
            conversation_id = msg.get("conversation_id") or conversation_id
            try:
                async for event in service.chat_stream(
                    question, conversation_id, tenant_id
                ):
                    await _send(ws, event["event"], event["data"])
            except Exception as exc:  # noqa: BLE001
                await _send(ws, "error", {"detail": str(exc)})
                break
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        await _send(ws, "error", {"detail": "WebSocket 内部错误"})


@router.websocket("/api/v1/ws/notify")
async def ws_notify(ws: WebSocket) -> None:
    """通知实时推送: 建立连接后, 有新通知即推送 notification 事件。

    认证关闭(演示模式)返回匿名用户(无 user_id), 无个人通知可推, 直接关闭。
    客户端断开后由 hub 清理连接。
    """
    await ws.accept()
    user = await get_ws_user(ws)
    user_id = user.get("id", "")
    if not user_id:
        await ws.close(code=1000)
        return
    from app.services.ws_hub import get_notify_hub

    hub = get_notify_hub()
    tenant_id = current_tenant_id(user)
    hub.connect(ws, tenant_id, user_id)
    try:
        # 保持连接存活: 忽略客户端消息(可选心跳); 断开即退出
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(ws, tenant_id, user_id)