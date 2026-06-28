"""工具注册表 —— 定义 agent 通过 Tool Calling 可用的所有工具。"""

from dataclasses import dataclass
from typing import Callable, Any
from .chart_tool import ChartTool, chart_tool


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict  # JSON schema
    handler: Callable

    def to_openai_format(self) -> dict:
        """转换为 OpenAI function calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """agent 可用工具的注册表。"""

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._setup_defaults()

    def register(self, tool: ToolDefinition):
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    def to_openai_tools(self) -> list[dict]:
        """以 OpenAI function calling 格式返回所有工具。"""
        return [t.to_openai_format() for t in self._tools.values()]

    async def execute(self, name: str, **kwargs) -> Any:
        tool = self.get(name)
        if not tool:
            raise ValueError(f"Unknown tool: {name}")
        import asyncio
        if asyncio.iscoroutinefunction(tool.handler):
            return await tool.handler(**kwargs)
        return tool.handler(**kwargs)

    def _setup_defaults(self):
        # 图表生成工具
        self.register(ToolDefinition(
            name="generate_chart",
            description=(
                "Generate a chart specification from query results. "
                "Use this when the user asks about trends, comparisons, "
                "distributions, or when a visual representation is more "
                "helpful than a table."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The original user question",
                    },
                    "columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Column names from the query result",
                    },
                    "rows": {
                        "type": "array",
                        "items": {"type": "array"},
                        "description": "Row data from the query result",
                    },
                    "chart_type_hint": {
                        "type": "string",
                        "enum": ["bar", "line", "pie", "table", "kpi"],
                        "description": "Optional hint for chart type",
                    },
                },
                "required": ["question", "columns", "rows"],
            },
            handler=self._handle_chart,
        ))

        # SQL 执行工具
        self.register(ToolDefinition(
            name="execute_sql",
            description=(
                "Execute a SQL query against the configured data source. "
                "Returns columns, rows, and execution metadata."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "The SQL query to execute"},
                    "source_id": {"type": "string", "description": "Data source ID"},
                },
                "required": ["sql"],
            },
            handler=self._handle_sql,
        ))

        # Python 代码执行工具（沙箱）
        self.register(ToolDefinition(
            name="execute_python",
            description=(
                "Execute Python code in an isolated sandbox for data analysis. "
                "The code has access to `input_data` dict and should assign "
                "results to a `result` variable. Useful for complex "
                "transformations that SQL cannot handle."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"},
                    "input_data": {"type": "object", "description": "Data available in the sandbox"},
                },
                "required": ["code"],
            },
            handler=self._handle_python,
        ))

    @staticmethod
    def _handle_chart(question: str, columns: list, rows: list, chart_type_hint: str = None):
        return chart_tool.generate(question, columns, rows, chart_type_hint).to_dict()

    @staticmethod
    async def _handle_sql(sql: str, source_id: str = "default"):
        # 在调用时委托给数据源注册表
        from ..datasources import create_default_registry
        registry = create_default_registry()
        result = await registry.execute(source_id, sql)
        return result.to_dict()

    @staticmethod
    async def _handle_python(code: str, input_data: dict = None):
        from ..sandbox import SandboxExecutor
        executor = SandboxExecutor()
        result = await executor.execute(code, input_data)
        return result.to_dict()


# 单例
tool_registry = ToolRegistry()
