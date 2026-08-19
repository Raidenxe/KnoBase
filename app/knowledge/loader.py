"""文档加载器: 将 md/txt/pdf/docx 说明书统一解析为带结构元数据的文本块。

TextBlock.section_path 记录章节路径(如 "3 安装部署 > 3.2 环境要求"),
TextBlock.page 记录 PDF 页码, 用于回答的来源引用定位。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

SUPPORTED_EXTS = {".md", ".markdown", ".txt", ".pdf", ".docx"}


@dataclass
class TextBlock:
    text: str
    section_path: str = "正文"
    page: int = -1
    heading: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class LoadedDoc:
    doc_name: str
    source_type: str
    blocks: List[TextBlock]


def load_file(path: str | Path) -> LoadedDoc:
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"不支持的文件类型 {ext}, 支持: {sorted(SUPPORTED_EXTS)}")
    if ext in {".md", ".markdown"}:
        return _load_markdown(path)
    if ext == ".txt":
        return _load_txt(path)
    if ext == ".pdf":
        return _load_pdf(path)
    return _load_docx(path)


# ---------------------------------------------------------------------------
# Markdown: 按 #/##/### 标题层级切分, 保留章节路径; 代码块整体保留
# ---------------------------------------------------------------------------
def _load_markdown(path: Path) -> LoadedDoc:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    blocks: List[TextBlock] = []
    header_stack: List[tuple[int, str]] = []  # (level, title)
    buf: List[str] = []
    in_code = False

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            blocks.append(
                TextBlock(
                    text=text,
                    section_path=" > ".join(t for _, t in header_stack) or "正文",
                    heading=header_stack[-1][1] if header_stack else "",
                )
            )
        buf.clear()

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            buf.append(line)
            continue
        if not in_code and stripped.startswith("#"):
            m = stripped.lstrip("#")
            level = len(stripped) - len(stripped.lstrip("#"))
            title = m.strip()
            flush()
            while header_stack and header_stack[-1][0] >= level:
                header_stack.pop()
            header_stack.append((level, title))
            continue
        buf.append(line)
    flush()
    return LoadedDoc(path.stem, "markdown", blocks)


def _load_txt(path: Path) -> LoadedDoc:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    blocks = [
        TextBlock(text=p.strip(), section_path="正文")
        for p in raw.split("\n\n") if p.strip()
    ]
    return LoadedDoc(path.stem, "txt", blocks or [TextBlock(text=raw, section_path="正文")])


def _load_pdf(path: Path) -> LoadedDoc:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    blocks: List[TextBlock] = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            blocks.append(TextBlock(text=text, section_path=f"第 {i} 页", page=i))
    return LoadedDoc(path.stem, "pdf", blocks)


def _load_docx(path: Path) -> LoadedDoc:
    import docx

    document = docx.Document(str(path))
    blocks: List[TextBlock] = []
    header_stack: List[str] = []
    buf: List[str] = []

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            blocks.append(
                TextBlock(text=text, section_path=" > ".join(header_stack) or "正文")
            )
        buf.clear()

    for para in document.paragraphs:
        style = (para.style.name or "").lower()
        text = para.text.strip()
        if not text:
            continue
        if "heading" in style or style.startswith("标题"):
            flush()
            level = "".join(ch for ch in style if ch.isdigit()) or "1"
            depth = int(level)
            while len(header_stack) >= depth:
                header_stack.pop()
            header_stack.append(text)
            continue
        buf.append(text)
    flush()
    return LoadedDoc(path.stem, "docx", blocks)
