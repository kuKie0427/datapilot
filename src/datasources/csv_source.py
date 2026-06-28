"""CSV 数据源适配器 —— 基于 DuckDB 的嵌入式 OLAP 引擎。

将上传的 CSV 文件注册为 DuckDB 视图，支持完整的 SQL 子集
（JOIN / GROUP BY / 窗口函数 / 子查询 / CTE），
彻底替代原正则解析器方案。
"""

import os
import re
import time
import asyncio
from typing import Optional
from .base import BaseAdapter, SchemaInfo, TableInfo, ColumnInfo, QueryResult


# DuckDB 类型到展示类型的映射
_DUCK_TYPE_MAP = {
    "BIGINT": "integer",
    "INTEGER": "integer",
    "SMALLINT": "integer",
    "TINYINT": "integer",
    "DOUBLE": "float",
    "FLOAT": "float",
    "DECIMAL": "decimal",
    "VARCHAR": "string",
    "TEXT": "string",
    "BOOLEAN": "boolean",
    "DATE": "date",
    "TIMESTAMP": "datetime",
    "TIME": "time",
}

# 合法表名/视图名白名单：仅字母数字下划线，防 SQL 注入
_SAFE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CSVAdapter(BaseAdapter):
    """CSV 文件的适配器 —— 使用 DuckDB 作为查询引擎。

    特性：
    - 完整 SQL 支持（JOIN / GROUP BY / 窗口函数 / 子查询 / CTE）
    - 流式扫描，不全部加载进内存，支持大数据集
    - 列式存储 + 向量化执行，亿行级 CSV 秒级返回
    - 自动类型推断
    """

    def __init__(self, source_id: str, csv_path: str, table_name: str = None):
        super().__init__(source_id, "csv")
        self.csv_path = csv_path
        # 表名白名单校验：只允许字母数字下划线，防 SQL 注入
        raw_name = table_name or os.path.splitext(os.path.basename(csv_path))[0]
        if not _SAFE_NAME_RE.match(raw_name):
            # 含非法字符 → 替换为下划线，确保不破坏 SQL
            safe = re.sub(r"[^A-Za-z0-9_]", "_", raw_name)
            if not safe or not _SAFE_NAME_RE.match(safe):
                safe = "csv_data"
            # 防止首字符为数字
            if safe[0].isdigit():
                safe = f"t_{safe}"
            self.table_name = safe
        else:
            self.table_name = raw_name
        self._con = None
        self._schema_cache: Optional[SchemaInfo] = None
        self._registered = False

    def _get_duckdb(self):
        """延迟导入并创建 DuckDB 连接。"""
        if self._con is None:
            try:
                import duckdb
            except ImportError as e:
                raise RuntimeError(
                    "duckdb is required for CSV source. Install with: pip install duckdb"
                ) from e
            # 每个适配器一个内存连接，互不干扰
            self._con = duckdb.connect(database=":memory:")
        return self._con

    def _ensure_view(self):
        """把 CSV 注册为视图，仅需注册一次。"""
        if self._registered:
            return
        con = self._get_duckdb()
        # 表名已通过 _SAFE_NAME_RE 校验，可安全拼接
        # csv_path 用 DuckDB 参数化查询，防路径注入
        # read_csv_auto 自动推断类型与编码
        con.execute(
            f'CREATE OR REPLACE VIEW "{self.table_name}" AS '
            f"SELECT * FROM read_csv_auto(?, header=true, ignore_errors=true)",
            [self.csv_path],
        )
        self._registered = True

    async def discover_schema(self) -> SchemaInfo:
        if self._schema_cache:
            return self._schema_cache
        loop = asyncio.get_event_loop()
        schema = await loop.run_in_executor(None, self._discover_sync)
        self._schema_cache = schema
        return schema

    def _discover_sync(self) -> SchemaInfo:
        self._ensure_view()
        con = self._get_duckdb()
        # DESCRIBE 返回 (column_name, column_type, null, key, default, extra)
        rows = con.execute(f'DESCRIBE SELECT * FROM "{self.table_name}"').fetchall()
        cols = []
        for r in rows:
            col_name = r[0]
            col_type = str(r[1]).upper()
            display_type = _DUCK_TYPE_MAP.get(col_type, col_type.lower())
            cols.append(ColumnInfo(
                name=col_name,
                dtype=display_type,
                nullable=str(r[2]).upper() == "YES" if len(r) > 2 else True,
            ))
        # 取行数（COUNT 较快）
        try:
            row_count = con.execute(f'SELECT COUNT(*) FROM "{self.table_name}"').fetchone()[0]
        except Exception:
            row_count = None
        table = TableInfo(
            name=self.table_name,
            columns=cols,
            row_count=row_count,
            description=f"CSV file: {os.path.basename(self.csv_path)} (via DuckDB)",
        )
        return SchemaInfo(
            source_id=self.source_id,
            source_type="csv",
            tables=[table],
        )

    async def execute_query(self, query: str, params: dict = None) -> QueryResult:
        """对 CSV 执行 SQL 查询。

        支持：SELECT / JOIN / GROUP BY / 窗口函数 / 子查询 / CTE / UNION 等
        标准 SQL 语法。表名使用 self.table_name（注册为视图）。

        若 LLM 生成的 SQL 用了别的表名（如 "table" / "data"），
        会自动替换为正确的视图名。
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._execute_sync, query)

    def _execute_sync(self, query: str) -> QueryResult:
        start = time.time()
        try:
            self._ensure_view()
            con = self._get_duckdb()

            # 容错：若 SQL 引用了非 self.table_name 的表名，做替换
            normalized = self._normalize_table_ref(query)

            # 执行并取结果
            rel = con.execute(normalized)
            columns = [d[0] for d in rel.description] if rel.description else []
            rows = rel.fetchall()
            # DuckDB 返回的可能是 numpy/tuple，统一转 list + Python 原生类型
            rows = [[self._native(v) for v in row] for row in rows]

            elapsed = (time.time() - start) * 1000
            return QueryResult(
                success=True,
                columns=columns,
                rows=rows,
                row_count=len(rows),
                execution_time_ms=elapsed,
            )
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            return QueryResult(
                success=False,
                error=str(e),
                execution_time_ms=elapsed,
            )

    def _normalize_table_ref(self, sql: str) -> str:
        """若 SQL 中表名不是 self.table_name，用 AST 替换。

        LLM 经常用 "FROM table" / "FROM data" / "FROM t" 等占位符，
        用 sqlglot AST 精确定位 Table 节点替换，避免正则误伤列名/别名。
        """
        try:
            import sqlglot
            from sqlglot import exp
            parsed = sqlglot.parse_one(sql, dialect="duckdb")
            # 收集所有非 CTE 名的 Table 节点
            cte_names = {c.alias_or_name for c in parsed.find_all(exp.CTE)}
            changed = False
            for table in parsed.find_all(exp.Table):
                tname = table.name
                # 跳过 CTE 名引用
                if tname in cte_names:
                    continue
                if tname != self.table_name:
                    # 替换为正确的表名
                    table.set("this", exp.to_identifier(self.table_name))
                    changed = True
            if not changed:
                return sql
            return parsed.sql(dialect="duckdb")
        except Exception:
            # AST 解析失败时回退到原 SQL（让 DuckDB 报错，触发纠错循环）
            return sql

    @staticmethod
    def _native(val):
        """将 numpy / DuckDB 原生类型转换为 Python 原生类型。"""
        if val is None:
            return None
        # numpy 标量
        if hasattr(val, "item"):
            return val.item()
        # datetime/date 等 duckdb 类型已是 Python 原生
        return val

    async def test_connection(self) -> bool:
        return os.path.exists(self.csv_path)

    def close(self):
        """关闭 DuckDB 连接，释放资源。"""
        if self._con is not None:
            try:
                self._con.close()
            except Exception:
                pass
            self._con = None
            self._registered = False
