"""飞书 / 钉钉机器人接入: 接收消息事件回调, 校验签名, 转交问答链, 回复结果。

飞书(Feishu):
    - URL 验证(url_verification): 匹配 verification_token 后原样回 challenge
    - 事件回调(im.message.receive_v1): 解析文本内容 → 调用 chat_service → 通过
      应用接口(tenant_access_token + im/v1/messages)回复。可在飞书「事件订阅」
      中配置请求地址为/公开地址/api/v1/bots/feishu/callback。

钉钉(DingTalk):
    - 自定义机器人「加签」回调: 校验 header/query 中 timestamp + sign(加签算法
      HMAC-SHA256), 解析 text 内容 → 调用 chat_service → 直接以机器人模板 JSON 返回。

均复用「检索增强 + 防幻觉」问答链, 以匿名用户 + 默认租户运行, 不依赖登录态。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from typing import Optional
from urllib.parse import quote_plus

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.services.chat_service import get_chat_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/bots", tags=["bots"])


def _settings():
    return get_settings()


async def _answer(question: str) -> str:
    """把机器人问题交给问答链(匿名用户, 默认租户), 返回整理后的回答文本。"""
    if not question:
        return "请发送要咨询的问题。"
    try:
        result = await get_chat_service().chat(question, None, _settings().default_tenant)
        return result.answer or "未生成回答。"
    except Exception as exc:  # noqa: BLE001
        logger.exception("机器人问答失败")
        return f"问答服务暂时不可用: {exc}"


# ---------------------------------------------------------------------------
# 飞书
# ---------------------------------------------------------------------------
async def _feishu_tenant_token(app_id: str, app_secret: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书 tenant_access_token 失败: {data.get('msg')}")
    return data["tenant_access_token"]


async def _feishu_reply(receive_id: str, text: str) -> None:
    """调用飞书 im/v1/messages 发送文本回复(receive_id 为发送者 open_id)。"""
    s = _settings()
    if not s.feishu_app_id or not s.feishu_app_secret:
        logger.warning("未配置飞书 app_id/app_secret, 跳过回复")
        return
    token = await _feishu_tenant_token(s.feishu_app_id, s.feishu_app_secret)
    payload = {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text[:4000]}, ensure_ascii=False),
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        data = r.json()
    if data.get("code") != 0:
        logger.warning("飞书回复失败: %s", data.get("msg"))


def _feishu_verify_token(body: dict) -> bool:
    token = body.get("token") or body.get("header", {}).get("token", "")
    expect = _settings().feishu_verification_token
    if not expect:
        return True  # 未配置验证 token 时不校验
    return token == expect


def _feishu_extract_text(body: dict) -> tuple[Optional[str], Optional[str]]:
    """从飞书事件体提取 (文本, 发送者 open_id)。仅处理文本消息。"""
    event = body.get("event") or {}
    message = event.get("message") or {}
    msg_type = message.get("message_type", "")
    if msg_type != "text":
        return None, None
    content_str = message.get("content", "{}")
    try:
        content = json.loads(content_str)
    except json.JSONDecodeError:
        return None, None
    text = content.get("text", "") or ""
    # 去除 @机器人 提及(短横线包裹的 at 元素可能直接含在 text 中)
    text = text.replace("\u200b", "").strip()
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    open_id = sender_id.get("open_id", "")
    return (text or None), open_id


@router.post("/feishu/callback", summary="飞书事件订阅回调")
async def feishu_callback(request: Request):
    body: dict = await request.json()
    # 1) URL 验证
    if body.get("type") == "url_verification":
        if not _feishu_verify_token(body):
            raise HTTPException(403, "verification token 校验失败")
        return JSONResponse({"challenge": body.get("challenge", "")})

    # 2) 事件回调: 校验 token 是否匹配
    if not _feishu_verify_token(body):
        raise HTTPException(403, "verification token 校验失败")

    text, open_id = _feishu_extract_text(body)
    if text is not None and open_id:
        # 异步回复: 在后台任务中调用问答链, 避免回调超时(飞书要求 3s 内响应)
        import asyncio

        # 先返回空 200, 再异步生成回复
        async def _reply():
            try:
                await _feishu_reply(open_id, await _answer(text))
            except Exception:  # noqa: BLE001
                logger.exception("飞书异步回复失败")

        asyncio.ensure_future(_reply())

    return {"code": 0}


# ---------------------------------------------------------------------------
# 钉钉
# ---------------------------------------------------------------------------
def _dingtalk_sign(timestamp: str, secret: str) -> str:
    """钉钉机器人「加签」算法: base64(HMAC-SHA256(timestamp\nsecret))。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    return quote_plus(base64.b64encode(digest))


def _dingtalk_verify(request: Request, body: dict) -> bool:
    secret = _settings().dingtalk_sign_secret
    if not secret:
        return True  # 未配置加签 secret 时不校验
    timestamp = request.query_params.get("timestamp") or body.get("timestamp", "")
    sign = request.query_params.get("sign") or body.get("sign", "")
    if not timestamp or not sign:
        return False
    try:
        expected = _dingtalk_sign(timestamp, secret)
    except Exception:  # noqa: BLE001
        return False
    return hmac.compare_digest(sign, expected)


@router.post("/dingtalk/callback", summary="钉钉机器人加签回调")
async def dingtalk_callback(request: Request):
    body: dict = await request.json()
    if not _dingtalk_verify(request, body):
        raise HTTPException(403, "签名校验失败")

    text = ((body.get("text") or {}).get("content") or "").strip()
    answer = await _answer(text)

    # 钉钉 custom robot 回调期望返回 markdown 消息模板
    return {
        "msgtype": "markdown",
        "markdown": {
            "title": "RAG 智能助手",
            "text": answer[:4000],
        },
    }


@router.post("/dingtalk/outgoing", summary="钉钉机器人 outgoing 回调")
async def dingtalk_outgoing(request: Request):
    """备用入口: 兼容 outgoing 机器人(同样携带 timestamp/sign)。"""
    return await dingtalk_callback(request)