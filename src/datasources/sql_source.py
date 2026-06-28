"""SQL 数据源适配器 —— 支持 SQLite 和 MySQL。"""

import os
import time
import sqlite3
import asyncio
from typing import Optional
from .base import BaseAdapter, SchemaInfo, TableInfo, ColumnInfo, QueryResult


class SQLAdapter(BaseAdapter):
    """SQLite 和 MySQL 数据源的统一适配器。

    根据连接字符串判断后端：
      - sqlite:///path/to/db.sqlite  → SQLite
      - mysql://user:pass@host:port/db  → MySQL
    """

    def __init__(self, source_id: str, connection_string: str, db_id: str = None):
        super().__init__(source_id, "sql")
        self.connection_string = connection_string
        self.db_id = db_id
        self._backend = self._detect_backend(connection_string)
        self._schema_cache: Optional[SchemaInfo] = None

    @staticmethod
    def _detect_backend(conn_str: str) -> str:
        if conn_str.startswith("sqlite://"):
            return "sqlite"
        elif conn_str.startswith("mysql://") or conn_str.startswith("mysql+"):
            return "mysql"
        # 回退：当作文件路径处理
        if conn_str.endswith(".sqlite") or conn_str.endswith(".db"):
            return "sqlite"
        return "sqlite"

    def _get_sqlite_path(self) -> str:
        if self.connection_string.startswith("sqlite://"):
            return self.connection_string[len("sqlite://"):]
        return self.connection_string

    def _get_mysql_config(self) -> dict:
        """解析 mysql://user:pass@host:port/database"""
        cs = self.connection_string
        for prefix in ("mysql://", "mysql+pymysql://", "mysql+mysqlconnector://"):
            if cs.startswith(prefix):
                cs = cs[len(prefix):]
                break
        # user:pass@host:port/database
        auth, _, rest = cs.partition("@")
        user, _, password = auth.partition(":")
        host_port, _, database = rest.partition("/")
        host, _, port = host_port.partition(":")
        return {
            "user": user,
            "password": password,
            "host": host or "localhost",
            "port": int(port) if port else 3306,
            "database": database,
        }

    def _connect_sqlite(self) -> sqlite3.Connection:
        path = self._get_sqlite_path()
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA query_only = ON;")
        return conn

    def _connect_mysql(self):
        import mysql.connector
        cfg = self._get_mysql_config()
        return mysql.connector.connect(**cfg)

    def _connect(self):
        if self._backend == "mysql":
            return self._connect_mysql()
        return self._connect_sqlite()

    # ---- 模式发现 ----

    async def discover_schema(self) -> SchemaInfo:
        if self._schema_cache:
            return self._schema_cache

        loop = asyncio.get_event_loop()
        schema = await loop.run_in_executor(None, self._discover_schema_sync)
        self._schema_cache = schema
        return schema

    def _discover_schema_sync(self) -> SchemaInfo:
        if self._backend == "sqlite":
            return self._discover_sqlite()
        return self._discover_mysql()

    def _discover_sqlite(self) -> SchemaInfo:
        conn = self._connect_sqlite()
        tables = []
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        table_names = [r[0] for r in cur.fetchall()]

        for tname in table_names:
            cols = []
            cur = conn.execute(f"PRAGMA table_info({tname});")
            for row in cur.fetchall():
                cols.append(ColumnInfo(
                    name=row[1],
                    dtype=row[2],
                    nullable=row[3] == 0,
                ))
            # 外键
            fks = []
            cur = conn.execute(f"PRAGMA foreign_key_list({tname});")
            for fk_row in cur.fetchall():
                fks.append({
                    "from_table": tname,
                    "from_column": fk_row[3],
                    "to_table": fk_row[2],
                    "to_column": fk_row[4],
                })
            tables.append(TableInfo(name=tname, columns=cols, foreign_keys=fks))

        conn.close()
        return SchemaInfo(source_id=self.source_id, source_type="sql", tables=tables)

    def _discover_mysql(self) -> SchemaInfo:
        conn = self._connect_mysql()
        cursor = conn.cursor()
        cursor.execute("SHOW TABLES;")
        table_names = [r[0] for r in cursor.fetchall()]
        tables = []

        for tname in table_names:
            cursor.execute(f"DESCRIBE `{tname}`;")
            cols = []
            for row in cursor.fetchall():
                cols.append(ColumnInfo(
                    name=row[0],
                    dtype=row[1],
                    nullable=row[2] == "YES",
                    description=row[3] or "",
                ))
            tables.append(TableInfo(name=tname, columns=cols))

        conn.close()
        return SchemaInfo(source_id=self.source_id, source_type="sql", tables=tables)

    # ---- 查询执行 ----

    async def execute_query(self, query: str, params: dict = None) -> QueryResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._execute_sync, query)

    def _execute_sync(self, query: str) -> QueryResult:
        start = time.time()
        try:
            conn = self._connect()
            cur = conn.execute(query) if self._backend == "sqlite" else conn.cursor()
            if self._backend == "mysql":
                cur.execute(query)
            rows = cur.fetchall()
            columns = (
                [d[0] for d in cur.description] if cur.description else []
            )
            conn.close()
            elapsed = (time.time() - start) * 1000
            return QueryResult(
                success=True,
                columns=columns,
                rows=[list(r) for r in rows],
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

    async def test_connection(self) -> bool:
        try:
            conn = self._connect()
            conn.close()
            return True
        except Exception:
            return False
