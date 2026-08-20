"""互联网知识导入: 抓取网页 → 提取正文 → 转为 Markdown → 走统一入库流水线。

用途: 用公开技术文档/官方文档补充私有知识库。
导入后的网页内容与本地说明书完全同权: 参与检索、携带来源引用、
受同一套防幻觉校验约束; 引用卡片中的"来源"行会保留原始 URL。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Tuple

import httpx


_NOISE_LINE = re.compile(
    r"^(?:home|docs|star|fork|next|previous|on this page|copy page|edit this page"
    r"|book a demo|try \w.*|v\d+(\.\w+)*(\.x)?|back to \w.*|skip to \w.*)$",
    re.IGNORECASE,
)


def _clean_lines(text: str) -> str:
    """行级降噪: 去导航/菜单/广告短行、连续重复行与多余空行"""
    seen: set = set()
    kept: list = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            kept.append("")
            continue
        if _NOISE_LINE.match(ln):
            continue
        # 短且无中文无数字的行基本是菜单项/按钮(如 "Get Started")
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", ln))
        if len(ln) < 30 and not has_cjk and not re.search(r"\d", ln) and len(ln.split()) <= 3:
            continue
        if ln in seen:  # 菜单在页面中重复出现
            continue
        seen.add(ln)
        kept.append(ln)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(kept))
    return out.strip()


def _html_to_markdown(html: str, url: str) -> Tuple[str, str]:
    """HTML → (标题, Markdown 正文)。非 HTML 原样保存。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(
        ["script", "style", "noscript", "nav", "header", "footer", "aside", "iframe", "form", "svg"]
    ):
        tag.decompose()
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    article = soup.find("article") or soup.find("main") or soup.body or soup
    text = article.get_text("\n")
    body = _clean_lines(text)
    md = f"# {title or url}\n\n> 网络来源: {url}\n\n{body}"
    return title or url, md


def fetch_url_as_markdown(url: str, timeout: float = 30.0) -> Tuple[str, str]:
    """抓取 URL, 返回 (建议文档名, markdown 内容)。"""
    if not re.match(r"^https?://", url):
        raise ValueError(f"仅支持 http/https 链接: {url}")
    resp = httpx.get(
        url,
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; RAGMaintenanceAssistant/1.0)"},
    )
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    if "html" in ctype:
        title, md = _html_to_markdown(resp.text, url)
    else:
        # 纯文本/Markdown 等直接保存
        title = re.sub(r"[?#].*$", "", url.rsplit("/", 1)[-1])[:60] or url
        md = f"# {title}\n\n> 网络来源: {url}\n\n{resp.text}"
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", title)[:50].strip("_") or "webpage"
    return safe, md


def save_webpage(url: str, downloads_dir: str | Path, timeout: float = 30.0) -> Path:
    """抓取并落盘为 Markdown 文件(幂等: 同 URL 内容 hash 命名)。"""
    downloads_dir = Path(downloads_dir)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    doc_name, md = fetch_url_as_markdown(url, timeout)
    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
    path = downloads_dir / f"{doc_name}_{digest}.md"
    path.write_text(md, encoding="utf-8")
    return path
