"""SQL 静态校验 —— 基于 sqlglot AST 的白名单 + 行数限制 + 行级过滤注入。

在 LLM 生成 SQL 后、执行前进行校验，防止：
- 非查询语句（INSERT/UPDATE/DELETE/DROP 等）
- 越权访问未授权表
- 笛卡尔积等危险查询
- 无 LIMIT 的全表扫描导致 OOM
- 通过 UNION 等绕过权限
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ValidationResult:
    """校验结果。"""
    ok: bool
    sql: str  # 校验后（可能被改写）的 SQL
    errors: list[str] = None
    warnings: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []
        if self.warnings is None:
            self.warnings = []


# 默认配置
DEFAULT_MAX_ROWS = 1000           # 普通用户最大返回行数
DEFAULT_ADMIN_MAX_ROWS = 100000   # admin 最大返回行数
DEFAULT_QUERY_TIMEOUT_MS = 10000  # 查询超时（仅供上层参考）

# 允许的语句类型（白名单）
_ALLOWED_STATEMENT_TYPES = {"select"}

# 禁止的函数（黑名单补充，主要防 DoS / 数据外泄）
# 全部小写存储；校验时也用 lower() 归一化，避免大小写绕过（SLEEP / Sleep / sleep）
_FORBIDDEN_FUNCTIONS = {
    # MySQL/MariaDB DoS
    "sleep", "benchmark", "get_lock", "release_lock", "is_free_lock",
    "is_used_lock",
    # MySQL 数据外泄
    "load_file", "into_outfile", "into_dumpfile", "dumpfile", "outfile",
    # MySQL/MariaDB UDF 命令执行
    "sys_eval", "sys_exec", "sys_get", "mysql_pconnect",
    # SQL Server（防方言误用）
    "xp_cmdshell",
    # PostgreSQL 命令执行
    "pg_sleep", "pg_read_file", "pg_ls_dir", "pg_read_binary_file",
    # 通用文件读取
    "readfile", "file_read",
}


class SQLValidator:
    """SQL 校验器 —— 在执行前对 LLM 生成的 SQL 做静态分析。

    使用 sqlglot 解析为 AST，在 AST 上做规则校验与改写，
    比字符串匹配/正则更可靠。
    """

    def __init__(
        self,
        allowed_tables: Optional[set[str]] = None,
        max_rows: int = DEFAULT_MAX_ROWS,
        dialect: str = "mysql",
    ):
        self.allowed_tables = allowed_tables      # None 表示不限制表
        self.max_rows = max_rows
        self.dialect = dialect

    def validate(self, sql: str) -> ValidationResult:
        """主入口：校验并改写 SQL。

        Returns:
            ValidationResult.ok=True 时可直接执行 result.sql。
        """
        try:
            import sqlglot
            from sqlglot import exp
        except ImportError:
            return ValidationResult(
                ok=False, sql=sql,
                errors=["sqlglot not installed; cannot validate SQL"],
            )

        errors: list[str] = []
        warnings: list[str] = []

        # 1. 解析
        try:
            parsed = sqlglot.parse_one(sql, dialect=self.dialect)
        except Exception as e:
            return ValidationResult(
                ok=False, sql=sql,
                errors=[f"SQL parse error: {e}"],
            )

        # 2. 语句类型白名单：只允许 SELECT
        stmt_type = parsed.key.lower()
        if stmt_type not in _ALLOWED_STATEMENT_TYPES:
            errors.append(
                f"Statement type '{stmt_type}' is not allowed; only SELECT is permitted"
            )
            return ValidationResult(ok=False, sql=sql, errors=errors)

        # 3. 表白名单
        if self.allowed_tables is not None:
            referenced = {t.name for t in parsed.find_all(exp.Table)}
            # 同时支持带反引号/引号的表名
            referenced_normalized = {t.strip("`'\"") for t in referenced}
            denied = referenced_normalized - self.allowed_tables
            if denied:
                errors.append(
                    f"Access denied to tables: {sorted(denied)}. "
                    f"Allowed: {sorted(self.allowed_tables)}"
                )

        # 4. 禁止 UNION（可绕过权限）
        if parsed.find(exp.Union):
            errors.append("UNION is not allowed (can bypass row-level security)")

        # 5. 禁止危险函数（大小写归一化后匹配）
        for func in parsed.find_all(exp.Func):
            func_name = str(func.key).lower()
            if func_name in _FORBIDDEN_FUNCTIONS:
                errors.append(f"Forbidden function: {func_name}")

        # 6. 笛卡尔积检测：FROM a, b 形式（无 JOIN 条件）
        #    严格判定笛卡尔积较复杂，这里保守处理：FROM 后有逗号分隔的多表且无 WHERE
        from_clause = parsed.args.get("from")
        if from_clause and isinstance(from_clause.this, exp.Tuple):
            if not parsed.args.get("where"):
                warnings.append("Possible cartesian product: multiple tables without WHERE clause")

        # 7. 强制 LIMIT：AST 上注入，而非字符串拼接
        # 同时检查 OFFSET，避免通过 OFFSET 遍历全表
        if not parsed.args.get("limit"):
            parsed = parsed.limit(self.max_rows)
            warnings.append(f"Auto-injected LIMIT {self.max_rows}")
        else:
            existing_limit = parsed.args.get("limit")
            try:
                limit_val = int(existing_limit.this.name)
                if limit_val > self.max_rows:
                    parsed = parsed.limit(self.max_rows)
                    warnings.append(
                        f"Original LIMIT {limit_val} exceeds max {self.max_rows}; clamped"
                    )
            except (AttributeError, ValueError):
                warnings.append("Could not parse existing LIMIT value; leaving as-is")

        # 7b. 检查 OFFSET：OFFSET + 多次查询可遍历全表，强制清零
        if parsed.args.get("offset"):
            errors.append("OFFSET is not allowed (can be used to scan full table)")

        if errors:
            return ValidationResult(ok=False, sql=sql, errors=errors, warnings=warnings)

        # 重新生成 SQL（标准化 + 已注入 LIMIT）
        rewritten = parsed.sql(dialect=self.dialect)
        return ValidationResult(ok=True, sql=rewritten, warnings=warnings)

    def inject_row_filter(self, sql: str, filter_condition: str) -> Optional[str]:
        """把行级过滤条件注入 SQL 的 WHERE 子句。

        用于 RBAC 行级权限：用户配置 region='east'，
        则 SELECT * FROM orders 自动变为
        SELECT * FROM orders WHERE region='east'。
        必须在 AST 上注入，否则 LLM 生成的 SQL 可绕过。

        Returns:
            改写后的 SQL 字符串；解析失败返回 None（fail-closed），
            调用方必须拒绝执行原 SQL，否则会绕过行级权限。
        """
        try:
            import sqlglot
            from sqlglot import exp
            parsed = sqlglot.parse_one(sql, dialect=self.dialect)
            # 把过滤条件解析为 WHERE 子句的条件节点（而非完整语句）
            # 用 "SELECT 1 WHERE <filter>" 解析，再提取 where.this，避免歧义
            wrapper = sqlglot.parse_one(
                f"SELECT 1 WHERE {filter_condition}", dialect=self.dialect
            )
            wrapper_where = wrapper.args.get("where")
            if wrapper_where is None:
                # filter_condition 解析失败 → fail-closed
                return None
            condition = wrapper_where.this
            if not isinstance(condition, exp.Expression):
                return None

            where = parsed.args.get("where")
            if where is None:
                parsed = parsed.where(condition)
            else:
                # 已有 WHERE：用 AND 合并，保证行级过滤不可被绕过
                existing = where.this
                parsed = parsed.where(exp.And(this=existing, expression=condition))
            return parsed.sql(dialect=self.dialect)
        except Exception:
            # 任何解析失败都返回 None，调用方必须拒绝执行
            return None


# 便捷函数
def validate_sql(
    sql: str,
    allowed_tables: Optional[set[str]] = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    dialect: str = "mysql",
) -> ValidationResult:
    """一次性校验入口。"""
    validator = SQLValidator(
        allowed_tables=allowed_tables,
        max_rows=max_rows,
        dialect=dialect,
    )
    return validator.validate(sql)
