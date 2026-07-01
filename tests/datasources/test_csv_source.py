"""CSV 数据源适配器测试 —— 覆盖表名白名单校验、首字符数字处理、
_normalize_table_ref 占位表名替换。
"""

import pytest

from src.datasources.csv_source import CSVAdapter, _SAFE_NAME_RE


# ============================================================
# 1. 表名白名单校验：恶意表名被清洗
# ============================================================

def test_table_name_with_semicolon_is_sanitized():
    """含分号的表名（SQL 注入尝试）应被清洗为合法标识符。"""
    adapter = CSVAdapter("s1", "/tmp/test.csv", table_name="foo; DROP TABLE bar")
    # 结果必须通过白名单正则
    assert _SAFE_NAME_RE.match(adapter.table_name)
    # 不含分号
    assert ";" not in adapter.table_name
    # 非法字符被替换为下划线
    assert adapter.table_name == "foo__DROP_TABLE_bar"


def test_table_name_with_quote_is_sanitized():
    """含引号的表名应被清洗。"""
    adapter = CSVAdapter("s1", "/tmp/test.csv", table_name="ta'ble")
    assert _SAFE_NAME_RE.match(adapter.table_name)
    assert "'" not in adapter.table_name
    assert adapter.table_name == "ta_ble"


def test_table_name_with_space_is_sanitized():
    """含空格的表名应被清洗。"""
    adapter = CSVAdapter("s1", "/tmp/test.csv", table_name="ta ble")
    assert _SAFE_NAME_RE.match(adapter.table_name)
    assert " " not in adapter.table_name
    assert adapter.table_name == "ta_ble"


def test_table_name_with_special_chars_is_sanitized():
    """含多种特殊字符的表名应被清洗。"""
    adapter = CSVAdapter("s1", "/tmp/test.csv", table_name="ta-ble!@#")
    assert _SAFE_NAME_RE.match(adapter.table_name)
    assert adapter.table_name == "ta_ble___"


def test_valid_table_name_is_kept_as_is():
    """合法表名应保持不变。"""
    adapter = CSVAdapter("s1", "/tmp/test.csv", table_name="orders")
    assert adapter.table_name == "orders"
    assert _SAFE_NAME_RE.match(adapter.table_name)


def test_table_name_underscore_prefix_is_valid():
    """以下划线开头的表名是合法的。"""
    adapter = CSVAdapter("s1", "/tmp/test.csv", table_name="_internal")
    assert adapter.table_name == "_internal"


def test_table_name_derived_from_filename_with_spaces():
    """从含空格的文件名推导表名时应被清洗。"""
    adapter = CSVAdapter("s1", "/tmp/my data.csv")
    assert _SAFE_NAME_RE.match(adapter.table_name)
    assert adapter.table_name == "my_data"


def test_all_special_chars_sanitized_to_underscores():
    """全部为特殊字符时替换为下划线（下划线是合法标识符字符）。"""
    adapter = CSVAdapter("s1", "/tmp/test.csv", table_name="!@#$%")
    # 特殊字符全部替换为 _，结果仍为合法标识符
    assert adapter.table_name == "_____"
    assert _SAFE_NAME_RE.match(adapter.table_name)


# ============================================================
# 2. 首字符为数字时加 t_ 前缀
# ============================================================

def test_table_name_starting_with_digit_gets_t_prefix():
    """首字符为数字的表名应加 t_ 前缀。"""
    adapter = CSVAdapter("s1", "/tmp/test.csv", table_name="123table")
    assert adapter.table_name == "t_123table"
    assert _SAFE_NAME_RE.match(adapter.table_name)


def test_table_name_starting_with_digit_is_sanitized():
    """首字符为数字的表名应被清洗为合法标识符（无论通过 t_ 前缀还是回退）。"""
    adapter = CSVAdapter("s1", "/tmp/test.csv", table_name="123table")
    # 无论机制如何，结果必须是合法表名
    assert _SAFE_NAME_RE.match(adapter.table_name)
    assert adapter.table_name[0].isalpha() or adapter.table_name[0] == "_"


# ============================================================
# 3. _normalize_table_ref 占位表名替换
# ============================================================

# _normalize_table_ref 内部使用 sqlglot，未安装时跳过
try:
    import sqlglot  # noqa: F401

    _HAS_SQLGLOT = True
except ImportError:
    _HAS_SQLGLOT = False

needs_sqlglot = pytest.mark.skipif(not _HAS_SQLGLOT, reason="sqlglot 未安装")


@needs_sqlglot
def test_normalize_table_replaces_placeholder_table():
    """LLM 常用的占位表名 'table' 应被替换为实际视图名。"""
    adapter = CSVAdapter("s1", "/tmp/test.csv", table_name="orders")
    normalized = adapter._normalize_table_ref("SELECT * FROM table WHERE x = 1")
    assert "orders" in normalized
    assert "FROM table" not in normalized or "FROM orders" in normalized


@needs_sqlglot
def test_normalize_table_replaces_placeholder_data():
    """占位表名 'data' 应被替换为实际视图名。"""
    adapter = CSVAdapter("s1", "/tmp/test.csv", table_name="orders")
    normalized = adapter._normalize_table_ref("SELECT * FROM data")
    assert "orders" in normalized


@needs_sqlglot
def test_normalize_table_keeps_correct_table_name():
    """SQL 中已使用正确表名时不应改变。"""
    adapter = CSVAdapter("s1", "/tmp/test.csv", table_name="orders")
    sql = "SELECT * FROM orders WHERE status = 1"
    normalized = adapter._normalize_table_ref(sql)
    # 正确表名不应被修改
    assert "orders" in normalized


@needs_sqlglot
def test_normalize_table_preserves_cte_names():
    """CTE 名不应被替换（只替换真实表引用）。"""
    adapter = CSVAdapter("s1", "/tmp/test.csv", table_name="orders")
    sql = "WITH t AS (SELECT 1) SELECT * FROM t"
    normalized = adapter._normalize_table_ref(sql)
    # CTE 名 't' 不应被替换为 'orders'
    # 但 CTE 内部的表引用（如果有）应被替换
    assert "WITH t AS" in normalized or "with t as" in normalized.lower()
