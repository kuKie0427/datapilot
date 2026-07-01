"""数据源适配器模块 —— 统一接口支持 SQL/CSV/API 数据源。"""

from .base import BaseAdapter, SchemaInfo, TableInfo, ColumnInfo, QueryResult
from .sql_source import SQLAdapter
from .csv_source import CSVAdapter
from .api_source import APIAdapter
from .registry import DataSourceRegistry, create_default_registry, get_shared_registry
