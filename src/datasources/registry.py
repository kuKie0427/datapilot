"""数据源注册表 —— 管理所有已注册的适配器。

添加新数据源只需实现 BaseAdapter 接口并在此注册即可。
"""

import os
import json
from typing import Optional
from .base import BaseAdapter, SchemaInfo
from .sql_source import SQLAdapter
from .csv_source import CSVAdapter
from .api_source import APIAdapter


class DataSourceRegistry:
    """所有数据源适配器的注册表。

    实现适配器模式：新数据源通过统一接口注册和访问。
    """

    def __init__(self):
        self._adapters: dict[str, BaseAdapter] = {}
        self._schemas: dict[str, SchemaInfo] = {}

    def register(self, adapter: BaseAdapter):
        self._adapters[adapter.source_id] = adapter

    def unregister(self, source_id: str):
        self._adapters.pop(source_id, None)
        self._schemas.pop(source_id, None)

    def get(self, source_id: str) -> Optional[BaseAdapter]:
        return self._adapters.get(source_id)

    def list_sources(self) -> list[dict]:
        return [a.describe() for a in self._adapters.values()]

    async def get_schema(self, source_id: str) -> Optional[SchemaInfo]:
        if source_id in self._schemas:
            return self._schemas[source_id]
        adapter = self.get(source_id)
        if not adapter:
            return None
        schema = await adapter.discover_schema()
        self._schemas[source_id] = schema
        return schema

    async def execute(self, source_id: str, query: str, params: dict = None):
        adapter = self.get(source_id)
        if not adapter:
            raise ValueError(f"Data source not found: {source_id}")
        return await adapter.execute_query(query, params or {})

    async def test_all(self) -> dict[str, bool]:
        results = {}
        for sid, adapter in self._adapters.items():
            results[sid] = await adapter.test_connection()
        return results


# ---- 默认注册表工厂 ----

def create_default_registry() -> DataSourceRegistry:
    """创建一个预加载了配置中默认数据源的注册表。"""
    from ..config import config

    registry = DataSourceRegistry()

    # 注册默认的 SQLite (Spider) 数据源
    sqlite_path = config.datasource.default_sqlite_path
    if os.path.exists(sqlite_path):
        registry.register(SQLAdapter(
            source_id="spider_sqlite",
            connection_string=f"sqlite://{sqlite_path}",
        ))

    # 如已配置则注册 MySQL 数据源
    if config.datasource.mysql_database:
        cs = (
            f"mysql://{config.datasource.mysql_user}:{config.datasource.mysql_password}"
            f"@{config.datasource.mysql_host}:{config.datasource.mysql_port}"
            f"/{config.datasource.mysql_database}"
        )
        registry.register(SQLAdapter(source_id="mysql_main", connection_string=cs))

    # 从上传目录注册 CSV 数据源
    csv_dir = config.datasource.csv_upload_dir
    if os.path.exists(csv_dir):
        for fname in os.listdir(csv_dir):
            if fname.endswith(".csv"):
                sid = f"csv_{os.path.splitext(fname)[0]}"
                registry.register(CSVAdapter(
                    source_id=sid,
                    csv_path=os.path.join(csv_dir, fname),
                ))

    # 从环境变量注册 API 数据源
    try:
        api_sources = json.loads(config.datasource.api_sources_json)
        for src in api_sources:
            registry.register(APIAdapter(
                source_id=src["id"],
                base_url=src["base_url"],
                endpoints=src.get("endpoints", []),
                auth=src.get("auth"),
                swagger_url=src.get("swagger_url"),
            ))
    except (json.JSONDecodeError, KeyError):
        pass

    return registry


# ---- 共享单例 ----
# nodes.py 与 routes.py 必须使用同一个 registry 实例，
# 否则运行时通过 POST /sources/register 注册的数据源在查询时找不到

_shared_registry: Optional[DataSourceRegistry] = None


def get_shared_registry() -> DataSourceRegistry:
    """获取全局共享的注册表单例。

    首次调用时懒加载默认数据源；后续调用返回同一实例，
    保证 API 层注册的数据源在 agent 查询时可见。
    """
    global _shared_registry
    if _shared_registry is None:
        _shared_registry = create_default_registry()
    return _shared_registry
