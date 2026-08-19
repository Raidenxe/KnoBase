"""项目文档在线阅读接口(仅 admin)。

面向"管理员统一管控"原则: 允许管理员在 Web 控制台直接浏览本项目自带的
README.md 与 docs/ 目录下的全部 Markdown 文档(运维/API/架构/部署/RBAC…)。

- 只读: 仅列出文件与返回 Markdown 原文, 不提供写操作
- 安全: 限定在项目 docs 目录与根 README.md, 拒绝任意路径穿越
- 权限: 两个端点均 admin_required
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.core.security import admin_required

router = APIRouter(prefix="/api/v1/project-docs", tags=["project-docs"])

# 项目根目录 = app/api/routes/../../../.. (本文件在 app/api/routes 下)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DOCS_DIR = _PROJECT_ROOT / "docs"


def _list_md_files() -> list[Path]:
    """收集 README.md 与 docs 下全部 markdown 文件。"""
    files: list[Path] = [_PROJECT_ROOT / "README.md"]
    if _DOCS_DIR.is_dir():
        for p in sorted(_DOCS_DIR.glob("*.md")):
            files.append(p)
    # 仅保留真实存在且确实位于白名单内的 markdown
    out = []
    for p in files:
        if p.exists() and p.is_file() and p.suffix.lower() in (".md", ".markdown"):
            out.append(p)
    return out


@router.get("", summary="项目文档列表(README + docs/*.md)", dependencies=[Depends(admin_required)])
def list_docs(user: dict = Depends(admin_required)) -> dict:
    docs = []
    for p in _list_md_files():
        try:
            mtime = p.stat().st_mtime
            size = p.stat().st_size
        except OSError:  # noqa: PERF203
            mtime = size = 0
        is_root = (Path(p).resolve() == (_PROJECT_ROOT / "README.md").resolve())
        docs.append({
            "name": p.name,
            "path": str(p.relative_to(_PROJECT_ROOT)),
            "size": size,
            "mtime": mtime,
            "root": is_root,
        })
    return {"docs": docs, "total": len(docs)}


@router.get("/{name}", summary="项目文档 Markdown 内容(admin)", dependencies=[Depends(admin_required)])
def get_doc(name: str, user: dict = Depends(admin_required)) -> dict:
    # 不允许路径分隔符穿越
    if "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(400, "非法文档名")
    target = None
    for p in _list_md_files():
        if p.name == name:
            target = p
            break
    if target is None:
        raise HTTPException(404, f"项目文档不存在: {name}")
    try:
        content = target.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"读取文档失败: {exc}") from exc
    return {"name": target.name, "path": str(target.relative_to(_PROJECT_ROOT)),
            "content": content}