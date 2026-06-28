"""语义层 prompt 构造 —— 让 LLM 输出 JSON plan 而非 SQL。"""

from .metrics import get_semantic_layer


PLANNER_SYSTEM_PROMPT = """You are a query planner for a semantic layer.
Given a natural language question, output a JSON query plan (NOT SQL).

The plan tells the system which metric to compute, which dimensions to group by,
and which filters to apply. The system will compile the plan to SQL deterministically.

Output format:
{
  "metric": "<metric_name>",
  "dimensions": ["<dim1>", "<dim2>"],
  "filters": [{"column": "<col>", "op": "=", "value": "<val>"}],
  "time_range": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
  "time_grain": "day|week|month|year",
  "order_by": "<col> ASC|DESC",
  "limit": 1000
}

Rules:
- Only use metric names from the "Available metrics" list.
- Only use dimensions from each metric's allowed dimensions.
- If the question doesn't mention a time range, leave time_range empty.
- If the question doesn't mention grouping, leave dimensions empty.
- Output ONLY the JSON, no markdown, no explanation.

Available metrics:
{metrics}

Question: {question}

Output the JSON plan:"""


def build_planner_prompt(question: str) -> str:
    """构造让 LLM 输出 plan 的 prompt。"""
    layer = get_semantic_layer()
    metrics_text = _render_metrics(layer.list_metrics())
    return PLANNER_SYSTEM_PROMPT.format(
        metrics=metrics_text,
        question=question,
    )


def _render_metrics(metrics: list) -> str:
    """渲染指标列表给 LLM 看。"""
    lines = []
    for m in metrics:
        parts = [f"- {m.name} ({m.type.value})"]
        if m.description:
            parts.append(f"  desc: {m.description}")
        if m.dimensions:
            parts.append(f"  dimensions: {', '.join(m.dimensions)}")
        if m.time_dim:
            parts.append(f"  time_dim: {m.time_dim}")
        if m.filter:
            parts.append(f"  filter: {m.filter}")
        lines.append("\n".join(parts))
    return "\n".join(lines) if lines else "(no metrics defined)"
