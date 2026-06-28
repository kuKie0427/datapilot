"""四阶段查询流水线的 LangGraph 状态定义。"""

from typing import TypedDict, Optional, Any, Annotated
from enum import Enum
from dataclasses import dataclass, field


class QueryStage(str, Enum):
    QUERY_REWRITE = "query_rewrite"     # 多轮对话查询改写
    INTENT = "intent_parsing"
    RAG = "rag_retrieval"
    GENERATION = "schema_aware_generation"
    SEMANTIC_COMPILE = "semantic_compile"  # 语义层 plan 编译
    EXECUTION = "execution_validation"
    CORRECTION = "error_correction"
    DONE = "done"
    ERROR = "error"


class OutputType(str, Enum):
    KPI = "kpi"
    TABLE = "table"
    CHART = "chart"
    SQL_ONLY = "sql_only"


@dataclass
class ReasoningStep:
    stage: str
    status: str  # done | running | pending | error
    detail: str = ""
    timestamp: float = 0.0


class AgentState(TypedDict, total=False):
    """在 LangGraph 流水线中流转的状态。

    每个节点都从此 dict 中读取和写入。
    """
    # 输入
    question: str
    source_id: str
    db_id: Optional[str]
    session_id: Optional[str]          # 多轮对话会话 ID
    user: Optional[Any]                # 已认证用户（auth.User），用于行级过滤与脱敏
    rewritten_question: Optional[str]  # 多轮对话改写后的问题

    # 阶段追踪
    current_stage: QueryStage
    steps: list[dict]

    # 意图解析输出
    intent: str  # aggregation | lookup | comparison | trend | unknown
    output_type: OutputType
    chart_type_hint: Optional[str]

    # RAG 输出
    few_shot_examples: list[dict]

    # 模式
    schema_text: str

    # 生成输出
    generated_sql: str
    candidate_sqls: list[str]
    semantic_plan: Optional[dict]    # 语义层 plan（metric + dimensions + filters）
    glossary_hits: list[dict]        # 命中的业务术语

    # 执行输出
    query_result: Optional[dict]
    execution_error: str

    # 纠错
    retry_count: int
    correction_history: list[dict]

    # 最终输出
    chart_spec: Optional[dict]
    success: bool
    error: str
    total_time_ms: float
