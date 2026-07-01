"""抽象数据源适配器 —— 定义所有数据源的契约。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional
import time


@dataclass
class ColumnInfo:
    """数据源中单个字段的元信息。"""
    name: str
    dtype: str
    nullable: bool = True
    description: str = ""


@dataclass
class TableInfo:
    """数据源中单张表/视图的元信息，包含字段、外键与行数。"""
    name: str
    columns: list[ColumnInfo] = field(default_factory=list)
    foreign_keys: list[dict] = field(default_factory=list)
    row_count: Optional[int] = None
    description: str = ""


@dataclass
class SchemaInfo:
    """数据源的模式发现结果。"""
    source_id: str
    source_type: str  # sql | csv | api
    tables: list[TableInfo] = field(default_factory=list)
    discovered_at: float = field(default_factory=time.time)

    def to_prompt_text(self) -> str:
        """将模式渲染为适合注入 LLM prompt 的文本。"""
        lines = []
        for tbl in self.tables:
            lines.append(f"TABLE {tbl.name}:")
            for col in tbl.columns:
                lines.append(f"  {col.name} ({col.dtype})")
            if tbl.foreign_keys:
                lines.append("  FOREIGN KEYS:")
                for fk in tbl.foreign_keys:
                    lines.append(f"    {fk['from_table']}.{fk['from_column']} -> {fk['to_table']}.{fk['to_column']}")
            if tbl.description:
                lines.append(f"  -- {tbl.description}")
            lines.append("")
        return "\n".join(lines)


@dataclass
class QueryResult:
    """对数据源执行查询的结果。"""
    success: bool
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    error: str = ""
    execution_time_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "error": self.error,
            "execution_time_ms": round(self.execution_time_ms, 2),
        }


class BaseAdapter(ABC):
    """所有数据源适配器的基类。

    实现新的数据源只需实现
    `discover_schema()` 和 `execute_query()`。
    """

    def __init__(self, source_id: str, source_type: str):
        self.source_id = source_id
        self.source_type = source_type

    @abstractmethod
    async def discover_schema(self) -> SchemaInfo:
        """发现并返回此数据源的模式。"""
        ...

    @abstractmethod
    async def execute_query(self, query: str, params: dict = None) -> QueryResult:
        """执行查询（DB 数据源为 SQL，CSV/API 为过滤表达式）。"""
        ...

    @abstractmethod
    async def test_connection(self) -> bool:
        """测试数据源是否可达。"""
        ...

    def describe(self) -> dict:
        """返回数据源的描述信息，用于注册表展示与日志输出。"""
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "class": self.__class__.__name__,
        }
