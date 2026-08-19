"""LangGraph 对话状态定义(在整个对话图中流转)"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class GraphState(TypedDict, total=False):
    # ---- 输入 ----
    conversation_id: str
    question: str                        # 用户原始问题
    history: List[Dict[str, str]]        # 多轮历史 [{"role","content"}]
    emit: Any                            # SSE 事件回调(异步)
    tenant_id: str                       # 当前租户(用于检索/生成隔离)
    trace_id: str                        # 全链路追踪 ID
    doc_version: Optional[int]           # 可选: 按版本文档过滤检索(None=全部)

    # ---- 中间产物 ----
    standalone_question: str             # 指代消解后的独立检索问题
    retrieved: List[Dict[str, Any]]      # Milvus 召回(含 score)
    filtered: List[Dict[str, Any]]       # 相关性精筛后的片段
    context_blocks: List[Dict[str, Any]] # 编号后的受限上下文

    # ---- 输出 ----
    answer: str
    citations: List[Dict[str, Any]]      # 来源引用
    verified: bool                       # 一致性校验结果
    verify_notes: str
    verify_feedback: str                 # 重试时反馈给生成节点
    retries: int
    refusal_reason: str
    route: str                           # 条件路由标记
    metrics: Dict[str, Any]              # 各阶段耗时
