"""语义层模块 —— 指标定义、plan 解析、SQL 编译。

让 LLM 只负责"选指标 + 选维度 + 选筛选"，不负责"怎么算"，
保证指标计算的一致性，复杂指标（ratio/rolling/cohort）用预定义模板。
"""

from .metrics import (
    Metric,
    MetricType,
    SemanticLayer,
    get_semantic_layer,
    load_metrics_yaml,
)
from .planner import (
    QueryPlan,
    parse_plan_from_llm,
    compile_plan_to_sql,
)
from .prompts import build_planner_prompt

__all__ = [
    "Metric",
    "MetricType",
    "SemanticLayer",
    "get_semantic_layer",
    "load_metrics_yaml",
    "QueryPlan",
    "parse_plan_from_llm",
    "compile_plan_to_sql",
    "build_planner_prompt",
]
