"""图表工具 —— 生成 ECharts 兼容的 JSON 用于可视化。

当用户问题暗示需要可视化/趋势/对比的答案而非普通表格时，
主 agent 通过 Tool Calling 调用此工具。
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ChartSpec:
    """返回给前端的声明式图表规范。"""
    chart_type: str  # bar | line | pie | table | kpi
    title: str
    data: list[dict] = field(default_factory=list)
    x_axis: str = ""
    y_axis: str = ""
    series: list[dict] = field(default_factory=list)
    echarts_option: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "chart_type": self.chart_type,
            "title": self.title,
            "data": self.data,
            "x_axis": self.x_axis,
            "y_axis": self.y_axis,
            "series": self.series,
            "echarts_option": self.echarts_option,
        }


class ChartTool:
    """根据查询结果生成图表规范。

    主 agent 调用此工具时传入：
      - columns：来自 QueryResult 的列名列表
      - rows：来自 QueryResult 的行值列表
      - question：原始用户问题（用于推断标题）
      - chart_type_hint：来自意图分类的可选提示

    返回一个 ChartSpec，由前端直接渲染。
    """

    # 暗示图表类型的关键词
    TREND_KEYWORDS = ["趋势", "变化", "走势", "trend", "over time", "daily", "weekly", "monthly", "近", "每天", "每周", "每月"]
    COMPARISON_KEYWORDS = ["排名", "对比", "比较", "top", "rank", "compare", "各", "每个"]
    RATIO_KEYWORDS = ["占比", "分布", "比例", "percentage", "distribution", "ratio"]
    KPI_KEYWORDS = ["多少", "总共", "总数", "平均", "how many", "total", "sum", "count", "avg"]

    def infer_chart_type(self, question: str, columns: list[str], rows: list[list]) -> str:
        """根据问题和数据形状推断最佳图表类型。"""
        q_lower = question.lower()

        # 单值 → KPI
        if len(rows) <= 1 and len(columns) <= 2:
            return "kpi"

        # 趋势 → 折线图
        if any(kw in q_lower for kw in self.TREND_KEYWORDS):
            return "line"

        # 比例 → 饼图
        if any(kw in q_lower for kw in self.RATIO_KEYWORDS):
            return "pie"

        # 对比 → 柱状图
        if any(kw in q_lower for kw in self.COMPARISON_KEYWORDS):
            return "bar"

        # 按数据形状启发式判断
        if len(rows) > 8:
            return "bar"
        if len(columns) == 2 and len(rows) <= 6:
            return "pie"

        return "table"

    def generate(
        self,
        question: str,
        columns: list[str],
        rows: list[list],
        chart_type_hint: str = None,
    ) -> ChartSpec:
        """根据查询结果生成图表规范。"""
        chart_type = chart_type_hint or self.infer_chart_type(question, columns, rows)
        title = self._infer_title(question)

        if chart_type == "kpi":
            return self._build_kpi(title, columns, rows)
        elif chart_type in ("bar", "line"):
            return self._build_cartesian(chart_type, title, columns, rows)
        elif chart_type == "pie":
            return self._build_pie(title, columns, rows)
        else:
            return self._build_table(title, columns, rows)

    def _infer_title(self, question: str) -> str:
        """将问题清理后作为图表标题。"""
        title = question.strip().rstrip("？?。.")
        if len(title) > 50:
            title = title[:50] + "..."
        return title

    def _build_kpi(self, title: str, columns: list[str], rows: list[list]) -> ChartSpec:
        kpis = []
        for i, col in enumerate(columns):
            value = rows[0][i] if rows and i < len(rows[0]) else None
            kpis.append({"label": col, "value": value})
        return ChartSpec(
            chart_type="kpi",
            title=title,
            data=kpis,
            echarts_option={},
        )

    def _build_cartesian(
        self, chart_type: str, title: str, columns: list[str], rows: list[list]
    ) -> ChartSpec:
        if not rows or not columns:
            return ChartSpec(chart_type=chart_type, title=title)

        # 第一列 = x 轴（通常是类别/时间）
        x_col = columns[0]
        x_data = [str(r[0]) for r in rows]

        # 其余列 = 系列
        series_list = []
        for i, col in enumerate(columns[1:], 1):
            values = []
            for r in rows:
                try:
                    v = float(r[i]) if r[i] is not None else 0
                except (ValueError, TypeError):
                    v = 0
                values.append(v)
            series_list.append({"name": col, "data": values})

        echarts_option = {
            "title": {"text": title, "textStyle": {"fontSize": 14}},
            "tooltip": {"trigger": "axis"},
            "xAxis": {"type": "category", "data": x_data, "axisLabel": {"rotate": 30}},
            "yAxis": {"type": "value"},
            "series": [
                {
                    "name": s["name"],
                    "type": chart_type,
                    "data": s["data"],
                    "itemStyle": {"borderRadius": [4, 4, 0, 0] if chart_type == "bar" else 0},
                }
                for s in series_list
            ],
            "grid": {"left": "3%", "right": "4%", "bottom": "10%", "containLabel": True},
        }

        return ChartSpec(
            chart_type=chart_type,
            title=title,
            x_axis=x_col,
            y_axis=", ".join(s["name"] for s in series_list),
            series=series_list,
            echarts_option=echarts_option,
        )

    def _build_pie(self, title: str, columns: list[str], rows: list[list]) -> ChartSpec:
        if not rows or not columns:
            return ChartSpec(chart_type="pie", title=title)

        # 第一列 = 名称，第二列 = 值
        name_col = columns[0]
        value_col = columns[1] if len(columns) > 1 else "value"

        pie_data = []
        for r in rows:
            try:
                v = float(r[1]) if len(r) > 1 and r[1] is not None else 0
            except (ValueError, TypeError):
                v = 0
            pie_data.append({"name": str(r[0]), "value": v})

        echarts_option = {
            "title": {"text": title, "left": "center"},
            "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
            "series": [{
                "type": "pie",
                "radius": "60%",
                "data": pie_data,
                "emphasis": {"itemStyle": {"shadowBlur": 10, "shadowOffsetX": 0}},
            }],
        }

        return ChartSpec(
            chart_type="pie",
            title=title,
            x_axis=name_col,
            y_axis=value_col,
            data=pie_data,
            echarts_option=echarts_option,
        )

    def _build_table(self, title: str, columns: list[str], rows: list[list]) -> ChartSpec:
        return ChartSpec(
            chart_type="table",
            title=title,
            data={"columns": columns, "rows": rows},
        )


# 单例
chart_tool = ChartTool()
