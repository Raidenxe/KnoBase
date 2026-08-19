"""pytest 全局夹具: 隔离的临时环境(独立 Milvus/历史库), 复用真实示例说明书。

必须在导入 app.* 之前设置环境变量(config 为 lru_cache 单例)。
"""

import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="rag_test_")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

os.environ["RAG_MILVUS_URI"] = os.path.join(_TMP, "test_milvus.db")
os.environ["RAG_HISTORY_DB_PATH"] = os.path.join(_TMP, "history.db")
os.environ["RAG_DOC_VERSIONS_DB_PATH"] = os.path.join(_TMP, "versions.db")
os.environ["RAG_SHARES_DB_PATH"] = os.path.join(_TMP, "shares.db")
os.environ["RAG_DOC_STATS_DB_PATH"] = os.path.join(_TMP, "doc_stats.db")
os.environ["RAG_AUTH_DB_PATH"] = os.path.join(_TMP, "auth.db")
os.environ["RAG_TRACE_DB_PATH"] = os.path.join(_TMP, "trace.db")
os.environ["RAG_AUDIT_DB_PATH"] = os.path.join(_TMP, "audit.db")
os.environ["RAG_LLM_USAGE_DB_PATH"] = os.path.join(_TMP, "llm_usage.db")
os.environ["RAG_PROMPT_STORE_PATH"] = os.path.join(_TMP, "prompts.json")
os.environ["RAG_DOC_META_DB_PATH"] = os.path.join(_TMP, "kb_meta.db")
os.environ["RAG_DOC_RECORDS_DB_PATH"] = os.path.join(_TMP, "kb_docs.db")
os.environ["RAG_NOTIFY_DB_PATH"] = os.path.join(_TMP, "notify.db")
os.environ["RAG_TICKET_DB_PATH"] = os.path.join(_TMP, "tickets.db")
os.environ["RAG_UPLOADS_DIR"] = os.path.join(_TMP, "uploads")
os.environ["RAG_AUTH_MODE"] = "off"  # 默认演示模式(auth 行为单独在 test_auth_rbac 中构造验证)
# 测试仅导入 3 份示例说明书(与生产 manuals 目录隔离, 避免个人笔记拖慢用例)
os.environ["RAG_MANUALS_DIR"] = str(Path(__file__).resolve().parent / "fixtures" / "manuals")
os.environ["RAG_AUTO_INGEST_ON_STARTUP"] = "false"
os.environ["RAG_LLM_PROVIDER"] = "mock"  # 测试强制离线模式, 不访问外部 LLM API

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def ingested():
    """一次性导入示例说明书, 返回入库结果"""
    from app.knowledge.pipeline import IngestPipeline

    results = IngestPipeline().ingest_directory(os.environ["RAG_MANUALS_DIR"])
    assert len(results) >= 3
    return results


@pytest.fixture(scope="session")
def client(ingested):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
