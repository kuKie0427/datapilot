"""REST API 数据源适配器 —— 查询外部 API 并规范化响应。

特性：
- 支持从 OpenAPI/Swagger 文档自动发现 Schema
- 无文档时支持探测式发现（发请求推断字段）
- Schema 漂移检测（定时重新探测 + diff）
"""

import time
import asyncio
import json as json_module
import httpx
from typing import Optional
from .base import BaseAdapter, SchemaInfo, TableInfo, ColumnInfo, QueryResult


class APIAdapter(BaseAdapter):
    """REST API 数据源的适配器。

    配置：
      - base_url：API 根 URL
      - endpoints：{name, path, method, params, headers} 列表
      - auth：可选的 {type: "bearer", token: "..."} 或 {type: "api_key", key: "...", header: "X-API-Key"}
      - swagger_url：可选的 OpenAPI 文档地址，用于自动发现 Schema

    查询格式：
      - "GET /users?limit=10"  → 从端点拉取数据
      - "POST /search {\"q\": \"abc\"}"  → 以 JSON body 发起 POST
    """

    def __init__(
        self,
        source_id: str,
        base_url: str,
        endpoints: list[dict],
        auth: dict,
        swagger_url: Optional[str] = None,
    ):
        super().__init__(source_id, "api")
        self.base_url = base_url.rstrip("/")
        self.endpoints = endpoints or []
        self.auth = auth or {}
        self.swagger_url = swagger_url
        self._schema_cache: Optional[SchemaInfo] = None

    def _build_headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.auth.get("type") == "bearer":
            headers["Authorization"] = f"Bearer {self.auth['token']}"
        elif self.auth.get("type") == "api_key":
            headers[self.auth.get("header", "X-API-Key")] = self.auth["key"]
        return headers

    async def discover_schema(self) -> SchemaInfo:
        """发现 Schema —— 优先 OpenAPI 文档，回退到探测式发现。"""
        if self._schema_cache:
            return self._schema_cache

        tables: list[TableInfo] = []

        # 优先级 1：OpenAPI/Swagger 文档解析
        if self.swagger_url:
            try:
                tables = await self._discover_from_openapi()
            except Exception:
                # 文档解析失败，回退到探测
                tables = []

        # 优先级 2：探测式发现（发请求推断字段）
        if not tables:
            tables = await self._discover_by_probing()

        # 优先级 3：用户手填 endpoints 的 fields
        if not tables:
            for ep in self.endpoints:
                cols = []
                for field_def in ep.get("fields", []):
                    cols.append(ColumnInfo(
                        name=field_def["name"],
                        dtype=field_def.get("type", "string"),
                        description=field_def.get("description", ""),
                    ))
                tables.append(TableInfo(
                    name=ep["name"],
                    columns=cols,
                    description=f"API endpoint: {ep.get('method', 'GET')} {ep['path']}",
                ))

        self._schema_cache = SchemaInfo(
            source_id=self.source_id,
            source_type="api",
            tables=tables,
        )
        return self._schema_cache

    async def _discover_from_openapi(self) -> list[TableInfo]:
        """从 OpenAPI/Swagger 文档解析 Schema。

        解析 paths 下每个端点的 method、参数、响应 schema，
        支持 $ref 引用递归解析。
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(self.swagger_url, headers=self._build_headers())
            resp.raise_for_status()
            spec = resp.json()

        # OpenAPI 3.x 在 components.schemas；Swagger 2.x 在 definitions
        components = spec.get("components", {}).get("schemas", {})
        definitions = spec.get("definitions", {})
        schema_pool = {**components, **definitions}

        tables: list[TableInfo] = []
        paths = spec.get("paths", {})
        for path, methods in paths.items():
            for method, op in methods.items():
                if method.lower() not in ("get", "post", "put", "delete"):
                    continue
                name = op.get("operationId") or op.get("summary") or path.strip("/").replace("/", "_")
                cols = self._extract_response_fields(op, schema_pool)
                description = op.get("summary", f"{method.upper()} {path}")
                tables.append(TableInfo(
                    name=name,
                    columns=cols,
                    description=description,
                ))
        return tables

    def _extract_response_fields(self, op: dict, schema_pool: dict) -> list[ColumnInfo]:
        """从 OpenAPI operation 的 200 响应里提取字段。"""
        cols: list[ColumnInfo] = []
        responses = op.get("responses", {})
        ok_resp = responses.get("200") or responses.get("default") or {}
        content = ok_resp.get("content", {})
        json_schema = content.get("application/json", {}).get("schema", {})
        if not json_schema:
            return cols

        # 解引用 $ref，并 unwrap items / array
        visited: set[str] = set()  # 防循环引用
        resolved = self._resolve_ref(json_schema, schema_pool, visited)
        items_schema = resolved.get("items")
        if items_schema:
            resolved = self._resolve_ref(items_schema, schema_pool, visited)

        # 从 properties 提取字段
        properties = resolved.get("properties", {})
        for field_name, field_schema in properties.items():
            cols.append(ColumnInfo(
                name=field_name,
                dtype=self._openapi_type_to_dtype(field_schema, schema_pool),
                nullable="required" not in resolved or field_name not in resolved.get("required", []),
                description=field_schema.get("description", ""),
            ))
        return cols

    def _resolve_ref(self, schema: dict, pool: dict, visited: Optional[set] = None) -> dict:
        """递归解析 $ref 引用。

        ref 形如 "#/components/schemas/User" 或 "#/definitions/User"。
        pool 是已合并的 schemas 字典（key 为 schema 名）。
        visited 记录已解析的 ref 路径，防止循环引用导致栈溢出。
        """
        if not isinstance(schema, dict):
            return schema
        ref = schema.get("$ref")
        if not ref:
            return schema
        if visited is None:
            visited = set()
        # 防循环：同一 ref 第二次出现就停止
        if ref in visited:
            return {}
        visited.add(ref)
        # 用 split 取 ref 的最后一段作为 schema 名（pool 已合并 components.schemas 与 definitions）
        # 形如 "#/components/schemas/User" → 取 "User"
        last_seg = ref.split("/")[-1]
        node = pool.get(last_seg)
        if not isinstance(node, dict):
            return schema
        # 节点本身可能又含 $ref，递归解析
        return self._resolve_ref(node, pool, visited)

    def _openapi_type_to_dtype(self, schema: dict, pool: dict) -> str:
        """OpenAPI 类型转展示类型。"""
        resolved = self._resolve_ref(schema, pool)
        t = resolved.get("type", "string")
        fmt = resolved.get("format", "")
        mapping = {
            "integer": "integer",
            "number": "float",
            "string": "string",
            "boolean": "boolean",
            "array": "array",
            "object": "object",
        }
        if t == "string" and fmt == "date-time":
            return "datetime"
        if t == "string" and fmt == "date":
            return "date"
        return mapping.get(t, "string")

    async def _discover_by_probing(self) -> list[TableInfo]:
        """探测式发现：对每个 endpoint 发一个 limit=1 的请求，推断字段。"""
        tables: list[TableInfo] = []
        async with httpx.AsyncClient(timeout=15) as client:
            for ep in self.endpoints:
                path = ep["path"]
                method = ep.get("method", "GET").upper()
                # 尝试带分页参数拉一条
                probe_path = self._inject_probe_params(path, ep)
                url = f"{self.base_url}{probe_path}"
                try:
                    if method == "GET":
                        resp = await client.get(url, headers=self._build_headers())
                    elif method == "POST":
                        resp = await client.post(url, headers=self._build_headers(),
                                                 json={"limit": 1})
                    else:
                        continue
                    if resp.status_code >= 400:
                        continue
                    data = resp.json()
                    cols = self._infer_fields_from_json(data)
                    tables.append(TableInfo(
                        name=ep["name"],
                        columns=cols,
                        description=f"API endpoint (probed): {method} {path}",
                    ))
                except Exception:
                    continue
        return tables

    def _inject_probe_params(self, path: str, ep: dict) -> str:
        """在 GET 路径上注入 limit=1 / page_size=1 探测参数。"""
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}limit=1&page_size=1"

    def _infer_fields_from_json(self, data) -> list[ColumnInfo]:
        """从 JSON 响应推断字段。

        支持两种常见结构：
        - 直接 list：[{...}, {...}]
        - 包装对象：{"data": [...], "total": 10}
        """
        # unwrap 包装
        if isinstance(data, dict):
            # 找第一个 list 类型的字段
            for k, v in data.items():
                if isinstance(v, list) and v:
                    data = v
                    break
            else:
                data = [data]
        if not isinstance(data, list) or not data:
            return []
        first = data[0]
        if not isinstance(first, dict):
            return [ColumnInfo(name="value", dtype="string")]
        cols = []
        for k, v in first.items():
            cols.append(ColumnInfo(
                name=k,
                dtype=self._python_type_to_dtype(v),
                nullable=v is None,
            ))
        return cols

    @staticmethod
    def _python_type_to_dtype(val) -> str:
        """从 Python 值推断展示类型。"""
        if val is None:
            return "string"
        if isinstance(val, bool):
            return "boolean"
        if isinstance(val, int):
            return "integer"
        if isinstance(val, float):
            return "float"
        return "string"

    async def execute_query(self, query: str, params: dict) -> QueryResult:
        """执行 API 查询。

        查询格式："GET /path?params" 或 "POST /path {json_body}"
        """
        start = time.time()
        try:
            method, _, rest = query.strip().partition(" ")
            method = method.upper()
            path, _, body_str = rest.strip().partition(" ")

            url = f"{self.base_url}{path}"
            headers = self._build_headers()
            json_body = None
            if body_str:
                json_body = json_module.loads(body_str)

            async with httpx.AsyncClient(timeout=30) as client:
                if method == "GET":
                    resp = await client.get(url, headers=headers)
                elif method == "POST":
                    resp = await client.post(url, headers=headers, json=json_body)
                elif method == "PUT":
                    resp = await client.put(url, headers=headers, json=json_body)
                elif method == "DELETE":
                    resp = await client.delete(url, headers=headers)
                else:
                    return QueryResult(success=False, error=f"Unsupported method: {method}")

            resp.raise_for_status()
            data = resp.json()

            # 规范化：如果是 dict，则包装为 list
            if isinstance(data, dict):
                # 若是 {data: [...]} 包装，则解包
                for k, v in data.items():
                    if isinstance(v, list):
                        data = v
                        break
                else:
                    data = [data]
            if not isinstance(data, list):
                data = [data]

            columns = list(data[0].keys()) if data else []
            rows = [[item.get(c) for c in columns] for item in data]

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

    async def test_connection(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(self.base_url, headers=self._build_headers())
                return resp.status_code < 500
        except Exception:
            return False

    async def refresh_schema(self) -> SchemaInfo:
        """强制重新发现 Schema（用于漂移检测）。"""
        self._schema_cache = None
        return await self.discover_schema()

    async def diff_schema(self) -> dict:
        """对比当前 Schema 与重新探测的 Schema，返回差异。

        返回：
          {"added": [...], "removed": [...], "changed": [...]}
          changed 形如 [table, column, old_dtype, new_dtype]
        """
        old = self._schema_cache
        new = await self.refresh_schema()
        if not old:
            return {"added": [], "removed": [], "changed": []}

        # 字段三元组：(table, column, dtype)
        old_fields = {(t.name, c.name, c.dtype) for t in old.tables for c in t.columns}
        new_fields = {(t.name, c.name, c.dtype) for t in new.tables for c in t.columns}

        # 用 (table, column) 作为身份 key，比对 dtype 变化
        old_map = {(t, c): d for (t, c, d) in old_fields}
        new_map = {(t, c): d for (t, c, d) in new_fields}
        old_keys = set(old_map.keys())
        new_keys = set(new_map.keys())

        added = [list(k) + [new_map[k]] for k in new_keys - old_keys]
        removed = [list(k) + [old_map[k]] for k in old_keys - new_keys]
        changed = []
        for k in old_keys & new_keys:
            if old_map[k] != new_map[k]:
                changed.append(list(k) + [old_map[k], new_map[k]])
        return {"added": added, "removed": removed, "changed": changed}
