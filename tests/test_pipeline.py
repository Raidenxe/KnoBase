"""知识处理流水线单元测试: 加载 / 分块 / 章节元数据"""

from pathlib import Path

from app.knowledge.loader import load_file
from app.knowledge.splitter import split_blocks

MANUALS = Path(__file__).resolve().parent / "fixtures" / "manuals"


def test_load_markdown_sections():
    doc = load_file(MANUALS / "智慧运维管理平台产品说明书.md")
    assert doc.source_type == "markdown"
    assert len(doc.blocks) > 5
    paths = [b.section_path for b in doc.blocks]
    assert any("3. 安装部署" in p for p in paths)
    assert any("3.3 默认账号" in p for p in paths)


def test_split_chunks_within_size_limit():
    doc = load_file(MANUALS / "DataGate数据采集网关产品说明书.md")
    chunks = split_blocks(doc.blocks, doc.doc_name, chunk_size=480, overlap=64)
    assert len(chunks) > 5
    for c in chunks:
        assert len(c.text) <= 560  # chunk_size + 容差
        assert c.doc_name == doc.doc_name
        assert c.section_path


def test_split_preserves_metadata():
    doc = load_file(MANUALS / "UniAuth统一身份认证系统说明书.md")
    chunks = split_blocks(doc.blocks, doc.doc_name, 480, 64)
    sla = [c for c in chunks if "维保" in c.section_path or "5.1" in c.section_path]
    assert sla, "应存在带章节元数据的分块"
    assert all(c.chunk_index >= 0 for c in chunks)
    idx = [c.chunk_index for c in chunks]
    assert idx == sorted(idx)


def test_unsupported_extension(tmp_path):
    import pytest

    bad = tmp_path / "manual.exe"
    bad.write_bytes(b"\x00\x01")
    with pytest.raises(ValueError):
        load_file(bad)
