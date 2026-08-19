"""文本分块器: 块内保序滑窗切分, 保留章节元数据, 支持重叠。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

from app.knowledge.loader import TextBlock


@dataclass
class Chunk:
    text: str
    section_path: str
    page: int
    doc_name: str
    chunk_index: int = 0
    meta: dict = field(default_factory=dict)


def _clean(text: str) -> str:
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()


def split_blocks(
    blocks: List[TextBlock], doc_name: str, chunk_size: int = 480, overlap: int = 64
) -> List[Chunk]:
    """将 TextBlock 切分为目标大小的 Chunk; 超长块按字符滑窗, 相邻重叠 overlap。"""
    chunks: List[Chunk] = []
    for block in blocks:
        text = _clean(block.text)
        if not text:
            continue
        if len(text) <= chunk_size:
            chunks.append(
                Chunk(
                    text=text,
                    section_path=block.section_path,
                    page=block.page,
                    doc_name=doc_name,
                )
            )
            continue
        step = max(chunk_size - overlap, 1)
        start = 0
        while start < len(text):
            piece = text[start : start + chunk_size].strip()
            if len(piece) >= int(chunk_size * 0.3):  # 丢弃过短的尾部碎屑
                chunks.append(
                    Chunk(
                        text=piece,
                        section_path=block.section_path,
                        page=block.page,
                        doc_name=doc_name,
                    )
                )
            if start + chunk_size >= len(text):
                break
            start += step
    for i, c in enumerate(chunks):
        c.chunk_index = i
    return chunks
