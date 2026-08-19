"""LangGraph 对话图构建(Corrective-RAG 防幻觉闭环):

    START → analyze_query → retrieve → grade_documents ─┬→ generate → verify ─┬→ END
                             (无相关资料→refuse)          ↑   (失败→带反馈重试) │
                                                        └────(重试耗尽)──→ refuse → END
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.graph.nodes import analyze_query, generate, grade_documents, refuse, retrieve, verify
from app.graph.state import GraphState


def _route_after_grading(state: GraphState) -> str:
    return "refuse" if state.get("route") == "refuse" else "generate"


def _route_after_verify(state: GraphState) -> str:
    if state.get("verified"):
        return END
    if state.get("route") == "refuse":
        return "refuse"
    if state.get("retries", 0) <= get_settings().generation_max_retries:
        return "generate"
    return "refuse"


@lru_cache
def build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("analyze_query", analyze_query)
    graph.add_node("retrieve", retrieve)
    graph.add_node("grade_documents", grade_documents)
    graph.add_node("generate", generate)
    graph.add_node("verify", verify)
    graph.add_node("refuse", refuse)

    graph.add_edge(START, "analyze_query")
    graph.add_edge("analyze_query", "retrieve")
    graph.add_edge("retrieve", "grade_documents")
    graph.add_conditional_edges(
        "grade_documents",
        _route_after_grading,
        {"generate": "generate", "refuse": "refuse"},
    )
    graph.add_edge("generate", "verify")
    graph.add_conditional_edges(
        "verify",
        _route_after_verify,
        {"generate": "generate", "refuse": "refuse", END: END},
    )
    graph.add_edge("refuse", END)
    return graph.compile()
