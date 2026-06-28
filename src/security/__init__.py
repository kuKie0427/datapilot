"""安全模块 —— SQL 校验、权限注入。"""

from .sql_validator import (
    SQLValidator,
    ValidationResult,
    validate_sql,
    DEFAULT_MAX_ROWS,
    DEFAULT_ADMIN_MAX_ROWS,
)

__all__ = [
    "SQLValidator",
    "ValidationResult",
    "validate_sql",
    "DEFAULT_MAX_ROWS",
    "DEFAULT_ADMIN_MAX_ROWS",
]
