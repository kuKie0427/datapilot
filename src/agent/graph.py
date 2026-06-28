"""LangGraph 状态机 —— 将各节点串联为四阶段流水线。

流水线：查询改写 → 意图识别 → RAG → 语义层（命中即编译） → 生成 → 执行 → （纠错循环） → 完成
"""

import time
from langgraph.graph import StateGraph, END
from .state import AgentState, QueryStage
from .nodes import (
    node_query_rewrite,
    node_intent_parsing,
    node_rag_retrieval,
    node_semantic_compile,
    node_schema_aware_generation,
    node_execution_validation,
    node_error_correction,
)


def _route_after_semantic(state: AgentState) -> str:
    """语义层节点后的路由：命中则直接执行，否则回退到生成。"""
    stage = state.get("current_stage", QueryStage.GENERATION)
    if stage == QueryStage.EXECUTION:
        return "execution"   # 命中语义层，跳过 generation
    return "generation"      # 回退到 schema-aware 生成


def _route_after_execution(state: AgentState) -> str:
    """执行后的路由：完成、纠错或错误。"""
    stage = state.get("current_stage", QueryStage.EXECUTION)
    if stage == QueryStage.DONE:
        return "done"
    elif stage == QueryStage.CORRECTION:
        return "correction"
    elif stage == QueryStage.ERROR:
        return "error"
    return "done"


def _route_after_correction(state: AgentState) -> str:
    """纠错后的路由：完成或回到执行。"""
    stage = state.get("current_stage", QueryStage.CORRECTION)
    if stage == QueryStage.DONE:
        return "done"
    elif stage == QueryStage.ERROR:
        return "error"
    # 回到执行阶段再试一次
    return "execution"


def build_graph():
    """构建并编译 LangGraph 流水线。"""
    graph = StateGraph(AgentState)

    # 添加节点
    graph.add_node("rewrite", node_query_rewrite)
    graph.add_node("intent", node_intent_parsing)
    graph.add_node("rag", node_rag_retrieval)
    graph.add_node("semantic", node_semantic_compile)
    graph.add_node("generation", node_schema_aware_generation)
    graph.add_node("execution", node_execution_validation)
    graph.add_node("correction", node_error_correction)

    # 设置入口：查询改写
    graph.set_entry_point("rewrite")

    # 线性边：rewrite → intent → rag → semantic
    graph.add_edge("rewrite", "intent")
    graph.add_edge("intent", "rag")
    graph.add_edge("rag", "semantic")

    # 条件边：semantic → execution（命中）/ generation（回退）
    graph.add_conditional_edges(
        "semantic",
        _route_after_semantic,
        {
            "execution": "execution",
            "generation": "generation",
        },
    )

    # 线性边：generation → execution
    graph.add_edge("generation", "execution")

    # 条件边：execution → done / correction / error
    graph.add_conditional_edges(
        "execution",
        _route_after_execution,
        {
            "done": END,
            "correction": "correction",
            "error": END,
        },
    )

    # 条件边：correction → done / error / 回到 execution
    graph.add_conditional_edges(
        "correction",
        _route_after_correction,
        {
            "done": END,
            "error": END,
            "execution": "execution",
        },
    )

    return graph.compile()


# 编译后的图单例
_pipeline = None


def get_pipeline():
    global _pipeline
    if _pipeline is None:
        _pipeline = build_graph()
    return _pipeline


async def run_pipeline(
    question: str,
    source_id: str = "default",
    db_id: str = None,
    user: "User" = None,
    session_id: str = None,
) -> AgentState:
    """针对用户问题运行完整的四阶段流水线。

    返回包含所有结果的最终 agent 状态。
    """
    start = time.time()
    pipeline = get_pipeline()

    initial_state: AgentState = {
        "question": question,
        "source_id": source_id,
        "db_id": db_id,
        "current_stage": QueryStage.QUERY_REWRITE,
        "steps": [],
        "retry_count": 0,
        "correction_history": [],
        "success": False,
        "error": "",
        "user": user,
        "session_id": session_id,
    }

    # 运行图
    result = await pipeline.ainvoke(initial_state)
    result["total_time_ms"] = (time.time() - start) * 1000
    return result
