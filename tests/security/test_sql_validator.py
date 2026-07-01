"""SQL 校验器测试 —— 覆盖语句类型白名单、表白名单、UNION 拦截、
危险函数大小写归一化、强制 LIMIT 注入与 clamp、OFFSET 拦截、
行级过滤注入 fail-closed 及 AND 合并、sqlglot 未安装时 fail-closed。
"""

import sys

import pytest

from src.security.sql_validator import SQLValidator, ValidationResult


# 检测 sqlglot 是否可用，用于按需跳过依赖 sqlglot 的测试
try:
    import sqlglot  # noqa: F401

    _HAS_SQLGLOT = True
except ImportError:
    _HAS_SQLGLOT = False

needs_sqlglot = pytest.mark.skipif(not _HAS_SQLGLOT, reason="sqlglot 未安装")


# ============================================================
# 1. 语句类型白名单：只允许 SELECT
# ============================================================

@needs_sqlglot
def test_statement_type_whitelist_rejects_insert():
    """INSERT 语句应被拒。"""
    v = SQLValidator(allowed_tables={"orders"})
    r = v.validate("INSERT INTO orders (a) VALUES (1)")
    assert not r.ok
    assert any("Statement type" in e for e in r.errors)


@needs_sqlglot
def test_statement_type_whitelist_rejects_update():
    """UPDATE 语句应被拒。"""
    v = SQLValidator(allowed_tables={"orders"})
    r = v.validate("UPDATE orders SET a = 1")
    assert not r.ok
    assert any("Statement type" in e for e in r.errors)


@needs_sqlglot
def test_statement_type_whitelist_rejects_delete():
    """DELETE 语句应被拒。"""
    v = SQLValidator(allowed_tables={"orders"})
    r = v.validate("DELETE FROM orders")
    assert not r.ok
    assert any("Statement type" in e for e in r.errors)


@needs_sqlglot
def test_statement_type_whitelist_rejects_drop():
    """DROP 语句应被拒。"""
    v = SQLValidator(allowed_tables={"orders"})
    r = v.validate("DROP TABLE orders")
    assert not r.ok
    assert any("Statement type" in e for e in r.errors)


@needs_sqlglot
def test_statement_type_whitelist_rejects_truncate():
    """TRUNCATE 语句应被拒。"""
    v = SQLValidator(allowed_tables={"orders"})
    r = v.validate("TRUNCATE TABLE orders")
    assert not r.ok
    assert any("Statement type" in e for e in r.errors)


@needs_sqlglot
def test_statement_type_whitelist_rejects_alter():
    """ALTER 语句应被拒。"""
    v = SQLValidator(allowed_tables={"orders"})
    r = v.validate("ALTER TABLE orders ADD COLUMN x INT")
    assert not r.ok
    assert any("Statement type" in e for e in r.errors)


@needs_sqlglot
def test_select_statement_is_allowed():
    """SELECT 语句应通过校验。"""
    v = SQLValidator(allowed_tables={"orders"})
    r = v.validate("SELECT * FROM orders LIMIT 10")
    assert r.ok


# ============================================================
# 2. 表白名单
# ============================================================

@needs_sqlglot
def test_table_whitelist_rejects_unauthorized():
    """越权访问未授权表应被拒。"""
    v = SQLValidator(allowed_tables={"orders"})
    r = v.validate("SELECT * FROM users")
    assert not r.ok
    assert any("Access denied" in e for e in r.errors)


@needs_sqlglot
def test_table_whitelist_allows_authorized():
    """授权表应通过校验。"""
    v = SQLValidator(allowed_tables={"orders"})
    r = v.validate("SELECT * FROM orders LIMIT 10")
    assert r.ok


# ============================================================
# 3. UNION 拦截
# ============================================================

@needs_sqlglot
def test_union_is_blocked():
    """UNION 应被拦截（可绕过行级权限）。"""
    v = SQLValidator(allowed_tables={"orders"})
    r = v.validate("SELECT * FROM orders UNION SELECT * FROM orders")
    assert not r.ok


@needs_sqlglot
def test_union_in_subquery_is_blocked():
    """子查询中的 UNION 也应被拦截。"""
    v = SQLValidator()  # 不限制表
    r = v.validate("SELECT * FROM (SELECT 1 AS x UNION SELECT 2 AS x) AS sub")
    assert not r.ok
    assert any("UNION" in e for e in r.errors)


# ============================================================
# 4. 危险函数大小写归一化
# ============================================================

@needs_sqlglot
def test_forbidden_function_case_normalization():
    """SLEEP/Sleep/sleep/BENCHMARK 均应被拦截（大小写归一化）。"""
    v = SQLValidator(allowed_tables={"orders"})
    for sql in [
        "SELECT SLEEP(1) FROM orders",
        "SELECT Sleep(1) FROM orders",
        "SELECT sleep(1) FROM orders",
        "SELECT BENCHMARK(1000000, MD5('x')) FROM orders",
        "SELECT benchmark(1000000, MD5('x')) FROM orders",
    ]:
        r = v.validate(sql)
        assert not r.ok, f"应拦截危险函数: {sql}"
        assert any("Forbidden function" in e for e in r.errors), f"应报告禁止函数: {sql}"


# ============================================================
# 5. 强制 LIMIT 注入与 clamp
# ============================================================

@needs_sqlglot
def test_limit_auto_injection():
    """无 LIMIT 时自动注入 max_rows。"""
    v = SQLValidator(allowed_tables={"orders"}, max_rows=100)
    r = v.validate("SELECT * FROM orders")
    assert r.ok
    assert any("Auto-injected LIMIT" in w for w in r.warnings)
    assert "LIMIT" in r.sql.upper()
    assert "100" in r.sql


@needs_sqlglot
def test_limit_within_max_is_kept():
    """已有 LIMIT 未超过 max_rows 时保持不变。"""
    v = SQLValidator(allowed_tables={"orders"}, max_rows=1000)
    r = v.validate("SELECT * FROM orders LIMIT 50")
    assert r.ok
    assert "50" in r.sql


@needs_sqlglot
def test_limit_clamp_when_exceeds_max():
    """已有 LIMIT 超过 max_rows 时应被 clamp 到 max_rows。"""
    v = SQLValidator(allowed_tables={"orders"}, max_rows=100)
    r = v.validate("SELECT * FROM orders LIMIT 10000")
    assert r.ok
    assert any("clamped" in w.lower() for w in r.warnings)
    # clamp 后 SQL 中 LIMIT 应为 100，而非 10000
    assert "100" in r.sql
    assert "10000" not in r.sql


# ============================================================
# 6. OFFSET 拦截
# ============================================================

@needs_sqlglot
def test_offset_is_blocked():
    """OFFSET 应被拦截（可遍历全表）。"""
    v = SQLValidator(allowed_tables={"orders"})
    r = v.validate("SELECT * FROM orders LIMIT 10 OFFSET 5")
    assert not r.ok
    assert any("OFFSET" in e for e in r.errors)


# ============================================================
# 7. inject_row_filter fail-closed
# ============================================================

@needs_sqlglot
def test_inject_row_filter_fail_closed_on_parse_error():
    """inject_row_filter 解析失败应返回 None（fail-closed）。"""
    v = SQLValidator()
    # 非法 SQL 无法解析
    result = v.inject_row_filter("SELECT FROM WHERE ;;", "region = 'east'")
    assert result is None


@needs_sqlglot
def test_inject_row_filter_fail_closed_on_bad_filter():
    """inject_row_filter 过滤条件非法时应返回 None。"""
    v = SQLValidator()
    # 过滤条件本身无法解析为合法 WHERE 条件
    result = v.inject_row_filter("SELECT * FROM orders", ";;;broken")
    assert result is None


# ============================================================
# 8. inject_row_filter 已有 WHERE 用 AND 合并
# ============================================================

@needs_sqlglot
def test_inject_row_filter_merges_existing_where_with_and():
    """已有 WHERE 时用 AND 合并行级过滤条件。"""
    v = SQLValidator()
    result = v.inject_row_filter(
        "SELECT * FROM orders WHERE status = 1",
        "region = 'east'",
    )
    assert result is not None
    upper = result.upper()
    # 原条件与注入条件均应存在
    assert "STATUS = 1" in upper or "STATUS=1" in upper
    assert "REGION = 'EAST'" in upper or "REGION='EAST'" in upper
    # 必须用 AND 连接
    assert "AND" in upper


@needs_sqlglot
def test_inject_row_filter_adds_where_when_absent():
    """无 WHERE 时直接添加行级过滤条件。"""
    v = SQLValidator()
    result = v.inject_row_filter(
        "SELECT * FROM orders",
        "region = 'east'",
    )
    assert result is not None
    assert "WHERE" in result.upper()
    assert "region = 'east'" in result.lower() or "region='east'" in result.lower()


# ============================================================
# 9. sqlglot 未安装时 fail-closed（mock 测试）
# ============================================================

def test_validate_fail_closed_when_sqlglot_missing(monkeypatch):
    """sqlglot 未安装时 validate 应 fail-closed 返回错误。"""
    # 模拟 sqlglot 未安装：将 sys.modules 中对应键置为 None，
    # 使 `import sqlglot` 抛出 ImportError
    monkeypatch.setitem(sys.modules, "sqlglot", None)
    monkeypatch.setitem(sys.modules, "sqlglot.exp", None)

    v = SQLValidator()
    r = v.validate("SELECT * FROM orders")
    assert not r.ok
    assert any("sqlglot not installed" in e for e in r.errors)
