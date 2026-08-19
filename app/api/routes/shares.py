"""会话分享公开接口(无鉴权只读): 供分享链接的接收方查看会话内容快照。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.services.shares import get_share_store

router = APIRouter(prefix="/api/v1", tags=["shares"])


@router.get("/shares/{token}", summary="公开只读查看分享的会话(JSON)")
def get_shared_conversation(token: str) -> dict:
    share = get_share_store().get(token)
    if not share:
        raise HTTPException(404, "分享链接不存在或已过期")
    return {
        "token": share["token"],
        "title": share["title"],
        "messages": share["messages"],
        "created_at": share["created_at"],
        "created_by": share["created_by"],
    }


@router.get("/share-html/{token}", summary="分享会话只读页面(HTML)", include_in_schema=False)
def shared_conversation_page(token: str):
    page = Path(__file__).resolve().parent.parent.parent / "static" / "share.html"
    if page.exists():
        return FileResponse(page)
    raise HTTPException(404, "分享页不存在")