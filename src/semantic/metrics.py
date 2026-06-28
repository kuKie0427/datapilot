"""指标定义 —— 语义层的核心数据结构。

参考 dbt Metrics / Cube.js / LookML 的设计：
- 指标 = 聚合函数 + 表 + 列 + 过滤条件
- 复杂指标（ratio）= 分子指标 / 分母指标
- 指标带可用维度列表，限制 LLM 只能选合法维度
"""

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MetricType(str, Enum):
    SIMPLE = "simple"          # 单一聚合：SUM(amount)
    COUNT_DISTINCT = "count_distinct"  # COUNT(DISTINCT user_id)
    RATIO = "ratio"            # 比率：分子 / 分母
    ROLLING = "rolling"        # 滚动窗口
    COHORT = "cohort"          # 同期群（占位，暂不实现编译）


@dataclass
class Metric:
    """指标定义。

    Attributes:
        name: 指标名（如 gmv）
        type: 指标类型
        sql: 聚合表达式（如 SUM(amount)），用于 simple/count_distinct
        table: 主表名
        filter: 行级过滤条件（SQL 片段）
        dimensions: 可下钻的维度列表
        time_dim: 时间维度列名
        numerator: 分子指标名（ratio 类型用）
        denominator: 分母指标名（ratio 类型用）
        window: 滚动窗口（如 30d，rolling 类型用）
        description: 业务描述
    """
    name: str
    type: MetricType = MetricType.SIMPLE
    sql: str = ""
    table: str = ""
    filter: str = ""
    dimensions: list[str] = field(default_factory=list)
    time_dim: str = ""
    numerator: str = ""
    denominator: str = ""
    window: str = ""
    description: str = ""


class SemanticLayer:
    """语义层 —— 指标注册表 + 检索。

    LLM 通过 build_planner_prompt 看到所有可用指标与维度，
    输出 QueryPlan，再由 compile_plan_to_sql 程序化编译为 SQL。
    """

    def __init__(self):
        self._metrics: dict[str, Metric] = {}

    def register(self, metric: Metric):
        self._metrics[metric.name] = metric

    def get(self, name: str) -> Optional[Metric]:
        return self._metrics.get(name)

    def list_metrics(self) -> list[Metric]:
        return list(self._metrics.values())

    def list_metric_names(self) -> list[str]:
        return list(self._metrics.keys())

    def load_yaml(self, path: str):
        """从 YAML 文件加载指标定义。"""
        try:
            import yaml
        except ImportError:
            raise RuntimeError("PyYAML required for semantic layer")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
        for item in data:
            metric = Metric(
                name=item["name"],
                type=MetricType(item.get("type", "simple")),
                sql=item.get("sql", ""),
                table=item.get("table", ""),
                filter=item.get("filter", ""),
                dimensions=item.get("dimensions", []),
                time_dim=item.get("time_dim", ""),
                numerator=item.get("numerator", ""),
                denominator=item.get("denominator", ""),
                window=item.get("window", ""),
                description=item.get("description", ""),
            )
            self.register(metric)

    def load_default(self):
        """加载默认指标集。"""
        defaults = [
            Metric(
                name="gmv",
                type=MetricType.SIMPLE,
                sql="SUM(amount)",
                table="orders",
                filter="status = 'paid'",
                dimensions=["region", "city", "category", "order_date"],
                time_dim="order_date",
                description="已支付订单金额总和",
            ),
            Metric(
                name="active_users",
                type=MetricType.COUNT_DISTINCT,
                sql="user_id",
                table="user_active_log",
                dimensions=["platform", "channel"],
                time_dim="active_date",
                description="去重活跃用户数",
            ),
            Metric(
                name="order_count",
                type=MetricType.SIMPLE,
                sql="COUNT(*)",
                table="orders",
                filter="status = 'paid'",
                dimensions=["region", "category", "order_date"],
                time_dim="order_date",
                description="已支付订单数",
            ),
            Metric(
                name="repurchase_rate",
                type=MetricType.RATIO,
                numerator="repurchase_users",
                denominator="total_buyers",
                table="orders",
                dimensions=["region"],
                time_dim="order_date",
                description="复购率 = 复购用户数 / 总购买用户数",
            ),
        ]
        for m in defaults:
            self.register(m)

        # 项目目录下的指标文件
        from ..config import PROJECT_ROOT
        metrics_file = PROJECT_ROOT / "metrics.yaml"
        if metrics_file.exists():
            try:
                self.load_yaml(str(metrics_file))
            except Exception:
                pass


# 单例
_layer: Optional[SemanticLayer] = None


def get_semantic_layer() -> SemanticLayer:
    global _layer
    if _layer is None:
        _layer = SemanticLayer()
        _layer.load_default()
    return _layer


def load_metrics_yaml(path: str) -> SemanticLayer:
    """加载指定路径的指标文件并替换单例。"""
    global _layer
    _layer = SemanticLayer()
    _layer.load_default()
    if os.path.exists(path):
        _layer.load_yaml(path)
    return _layer
