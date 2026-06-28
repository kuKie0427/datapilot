"""Plan 解析与 SQL 编译 —— 把 LLM 输出的 JSON plan 编译为 SQL。

这是语义层的核心：LLM 只输出"查什么指标 + 哪些维度 + 什么过滤"，
SQL 怎么写由本模块确定性地编译，保证一致性、可测试、可缓存。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Optional
from .metrics import Metric, MetricType, get_semantic_layer


# order_by 合法格式：列名 [ASC|DESC]，列名仅字母数字下划线
# 拒绝任何含 SQL 关键字、分号、引号注入的输入
_ORDER_BY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\s+(ASC|DESC))?$",
    re.IGNORECASE,
)


@dataclass
class FilterCondition:
    """过滤条件。"""
    column: str
    op: str          # =, !=, >, <, >=, <=, in, between
    value: object


@dataclass
class QueryPlan:
    """LLM 输出的查询计划。

    metric: 指标名
    dimensions: 下钻维度列表
    filters: 过滤条件列表
    time_range: 时间范围 {start, end}
    time_grain: 时间聚合粒度 day/week/month
    order_by: 排序字段
    limit: 行数限制
    """
    metric: str
    dimensions: list[str] = field(default_factory=list)
    filters: list[FilterCondition] = field(default_factory=list)
    time_range: dict = field(default_factory=dict)
    time_grain: str = ""
    order_by: str = ""
    limit: int = 1000

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "dimensions": self.dimensions,
            "filters": [f.__dict__ for f in self.filters],
            "time_range": self.time_range,
            "time_grain": self.time_grain,
            "order_by": self.order_by,
            "limit": self.limit,
        }


def parse_plan_from_llm(llm_output: str) -> Optional[QueryPlan]:
    """从 LLM 输出（JSON 字符串）解析 QueryPlan。"""
    try:
        # 去除 markdown 围栏
        text = llm_output.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)

        filters = [
            FilterCondition(column=f["column"], op=f["op"], value=f["value"])
            for f in data.get("filters", [])
        ]
        return QueryPlan(
            metric=data["metric"],
            dimensions=data.get("dimensions", []),
            filters=filters,
            time_range=data.get("time_range", {}),
            time_grain=data.get("time_grain", ""),
            order_by=data.get("order_by", ""),
            limit=int(data.get("limit", 1000)),
        )
    except Exception:
        return None


def compile_plan_to_sql(plan: QueryPlan, dialect: str = "mysql") -> Optional[str]:
    """把 QueryPlan 编译为 SQL。

    根据 metric 类型分派到不同编译器：
    - simple/count_distinct：直接聚合
    - ratio：分子/分母分别聚合后相除（CTE）
    - rolling：滚动窗口（占位）
    """
    layer = get_semantic_layer()
    metric = layer.get(plan.metric)
    if not metric:
        return None

    if metric.type in (MetricType.SIMPLE, MetricType.COUNT_DISTINCT):
        return _compile_simple(metric, plan, dialect)
    elif metric.type == MetricType.RATIO:
        return _compile_ratio(metric, plan, dialect)
    elif metric.type == MetricType.ROLLING:
        return _compile_rolling(metric, plan, dialect)
    return None


def _compile_simple(metric: Metric, plan: QueryPlan, dialect: str) -> str:
    """简单指标编译：SELECT dims, AGG(expr) FROM table WHERE ... GROUP BY dims."""
    # 聚合表达式
    if metric.type == MetricType.COUNT_DISTINCT:
        agg = f"COUNT(DISTINCT {metric.sql})"
    else:
        agg = metric.sql  # 已含聚合函数，如 SUM(amount)

    select_parts = list(plan.dimensions)
    select_parts.append(f"{agg} AS {metric.name}")

    # 时间粒度处理
    if plan.time_grain and metric.time_dim:
        time_expr = _time_grain_expr(metric.time_dim, plan.time_grain, dialect)
        if time_expr:
            # 替换原始时间维度为粒度表达式
            if metric.time_dim in select_parts:
                select_parts.remove(metric.time_dim)
            select_parts.insert(0, f"{time_expr} AS {metric.time_dim}")
            if metric.time_dim not in plan.dimensions:
                plan.dimensions.insert(0, metric.time_dim)

    select_clause = ", ".join(select_parts)
    from_clause = metric.table

    # WHERE
    where_parts = []
    if metric.filter:
        where_parts.append(f"({metric.filter})")
    for f in plan.filters:
        where_parts.append(_compile_filter(f, dialect))
    if plan.time_range:
        if plan.time_range.get("start") and metric.time_dim:
            where_parts.append(f"{metric.time_dim} >= '{plan.time_range['start']}'")
        if plan.time_range.get("end") and metric.time_dim:
            where_parts.append(f"{metric.time_dim} <= '{plan.time_range['end']}'")

    where_clause = " AND ".join(where_parts) if where_parts else ""

    # GROUP BY
    group_parts = [d for d in plan.dimensions]
    group_clause = ", ".join(group_parts) if group_parts else ""

    # ORDER BY（校验后使用，防注入）
    raw_order = plan.order_by or (f"{metric.name} DESC" if not plan.dimensions else "")
    order_clause = _safe_order_by(raw_order)

    sql = f"SELECT {select_clause}\nFROM {from_clause}"
    if where_clause:
        sql += f"\nWHERE {where_clause}"
    if group_clause:
        sql += f"\nGROUP BY {group_clause}"
    if order_clause:
        sql += f"\nORDER BY {order_clause}"
    sql += f"\nLIMIT {plan.limit}"
    return sql


def _compile_ratio(metric: Metric, plan: QueryPlan, dialect: str) -> str:
    """比率指标编译：用 CTE 分别计算分子分母，再相除。

    假设分子/分母本身也是已注册的 simple 指标。
    """
    layer = get_semantic_layer()
    num_metric = layer.get(metric.numerator)
    den_metric = layer.get(metric.denominator)
    if not num_metric or not den_metric:
        return None

    # 共用维度与 WHERE
    dims = plan.dimensions
    where_parts = []
    if metric.filter:
        where_parts.append(f"({metric.filter})")
    for f in plan.filters:
        where_parts.append(_compile_filter(f, dialect))
    where_clause = " AND ".join(where_parts) if where_parts else "1=1"
    group_clause = ", ".join(dims) if dims else ""

    # 分子 CTE
    num_agg = (f"COUNT(DISTINCT {num_metric.sql})"
               if num_metric.type == MetricType.COUNT_DISTINCT
               else num_metric.sql)
    num_select = (", ".join(dims + [f"{num_agg} AS num"]) if dims
                  else f"{num_agg} AS num")
    num_sql = f"SELECT {num_select} FROM {num_metric.table} WHERE {where_clause}"
    if group_clause:
        num_sql += f" GROUP BY {group_clause}"

    # 分母 CTE
    den_agg = (f"COUNT(DISTINCT {den_metric.sql})"
               if den_metric.type == MetricType.COUNT_DISTINCT
               else den_metric.sql)
    den_select = (", ".join(dims + [f"{den_agg} AS den"]) if dims
                  else f"{den_agg} AS den")
    den_sql = f"SELECT {den_select} FROM {den_metric.table} WHERE {where_clause}"
    if group_clause:
        den_sql += f" GROUP BY {group_clause}"

    # join key
    join_on = " AND ".join(f"n.{d} = d.{d}" for d in dims) if dims else "1=1"
    select_dims = ", ".join(f"n.{d}" for d in dims) if dims else ""
    final_select = (f"{select_dims}, " if select_dims else "") + \
                   "n.num, d.den, ROUND(n.num * 1.0 / NULLIF(d.den, 0), 4) AS " + metric.name

    sql = f"WITH num AS (\n{num_sql}\n),\nden AS (\n{den_sql}\n)\nSELECT {final_select}\nFROM num n JOIN den d ON {join_on}"
    raw_order = plan.order_by or f"{metric.name} DESC"
    order_clause = _safe_order_by(raw_order)
    if order_clause:
        sql += f"\nORDER BY {order_clause}"
    sql += f"\nLIMIT {plan.limit}"
    return sql


def _compile_rolling(metric: Metric, plan: QueryPlan, dialect: str) -> Optional[str]:
    """滚动窗口指标编译（占位实现：用窗口函数）。"""
    if not metric.window or not metric.time_dim:
        return None
    # 简化：用 ROWS BETWEEN N PRECEDING AND CURRENT ROW
    # 实际 rolling 需要根据 window 解析天数
    return None


def _compile_filter(f: FilterCondition, dialect: str) -> str:
    """编译单个过滤条件为 SQL 片段。

    字符串值会转义单引号（防 SQL 注入）。
    """
    # 列名白名单校验：只允许字母数字下划线
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", f.column):
        # 非法列名 → 跳过此过滤条件（返回永真，避免拼出坏 SQL）
        return "1=1"
    if f.op.lower() == "in":
        vals = f.value if isinstance(f.value, list) else [f.value]
        quoted = ", ".join(
            f"'{_escape_str(v)}'" if isinstance(v, str) else str(v) for v in vals
        )
        return f"{f.column} IN ({quoted})"
    elif f.op.lower() == "between":
        vals = f.value if isinstance(f.value, list) else [f.value, f.value]
        v0 = f"'{_escape_str(vals[0])}'" if isinstance(vals[0], str) else str(vals[0])
        v1 = f"'{_escape_str(vals[1])}'" if isinstance(vals[1], str) else str(vals[1])
        return f"{f.column} BETWEEN {v0} AND {v1}"
    else:
        v = f.value
        if isinstance(v, str):
            return f"{f.column} {f.op} '{_escape_str(v)}'"
        return f"{f.column} {f.op} {v}"


def _escape_str(s: str) -> str:
    """转义 SQL 字符串字面量中的单引号（标准 SQL 用两个单引号转义）。"""
    return s.replace("'", "''")


def _safe_order_by(order_by: str) -> str:
    """校验 order_by 格式，非法则返回空串。

    只允许 "列名" 或 "列名 ASC|DESC"，列名仅字母数字下划线。
    """
    if not order_by:
        return ""
    if not _ORDER_BY_RE.match(order_by.strip()):
        return ""
    return order_by.strip()


def _time_grain_expr(time_dim: str, grain: str, dialect: str) -> Optional[str]:
    """根据时间粒度生成时间截断表达式。"""
    grain = grain.lower()
    if dialect == "mysql":
        if grain == "day":
            return f"DATE({time_dim})"
        elif grain == "week":
            return f"YEARWEEK({time_dim})"
        elif grain == "month":
            return f"DATE_FORMAT({time_dim}, '%Y-%m')"
        elif grain == "year":
            return f"YEAR({time_dim})"
    elif dialect == "sqlite":
        if grain == "day":
            return f"DATE({time_dim})"
        elif grain == "month":
            return f"strftime('%Y-%m', {time_dim})"
        elif grain == "year":
            return f"strftime('%Y', {time_dim})"
    return None
