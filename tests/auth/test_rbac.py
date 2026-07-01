"""RBAC 权限测试 —— 覆盖资源通配符匹配、admin 动作通配、列脱敏。"""

import pytest

from src.auth.models import User, Role, Permission
from src.auth.rbac import _match_resource, check_permission, mask_columns


# ============================================================
# 1. _match_resource 通配符 * 与 prefix:*
# ============================================================

def test_match_resource_star_matches_anything():
    """通配符 * 匹配任意资源。"""
    assert _match_resource("*", "datasource:mysql") is True
    assert _match_resource("*", "anything") is True
    assert _match_resource("*", "") is True


def test_match_resource_prefix_wildcard():
    """prefix:* 匹配 prefix 自身及 prefix:xxx。"""
    # 匹配前缀下的子资源
    assert _match_resource("datasource:*", "datasource:mysql_main") is True
    assert _match_resource("datasource:*", "datasource:postgres") is True
    # 匹配前缀自身
    assert _match_resource("datasource:*", "datasource") is True
    # 不匹配其他前缀
    assert _match_resource("datasource:*", "other:thing") is False
    assert _match_resource("datasource:*", "datasource_other") is False


def test_match_resource_exact_match():
    """精确匹配：pattern == resource。"""
    assert _match_resource("datasource:mysql", "datasource:mysql") is True
    assert _match_resource("datasource:mysql", "datasource:postgres") is False


# ============================================================
# 2. check_permission admin 动作通配
# ============================================================

async def test_check_permission_admin_wildcards_any_action(auth_store):
    """admin 动作通配：拥有 admin 权限的用户对任意 action 都返回 True。"""
    # 准备权限点：resource=* action=admin
    auth_store.add_permission(Permission(
        id="admin:all", resource="*", action="admin", description="系统管理",
    ))
    # 准备角色
    auth_store.add_role(Role(
        name="admin", tenant_id="default", permissions=["admin:all"],
    ))
    # 准备用户
    user = User(
        id="u-admin", name="Admin", tenant_id="default", roles=["admin"],
    )
    auth_store.add_user(user)

    # admin 应对任意 resource + action 返回 True
    assert await check_permission(user, "datasource:mysql", "query") is True
    assert await check_permission(user, "datasource:mysql", "schema") is True
    assert await check_permission(user, "datasource:mysql", "delete") is True
    assert await check_permission(user, "anything", "anyaction") is True


async def test_check_permission_non_admin_denied_for_unauthorized_action(auth_store):
    """非 admin 用户对未授权 action 应返回 False。"""
    auth_store.add_permission(Permission(
        id="query:all", resource="datasource:*", action="query",
    ))
    auth_store.add_role(Role(
        name="viewer", tenant_id="default", permissions=["query:all"],
    ))
    user = User(id="u-viewer", name="Viewer", tenant_id="default", roles=["viewer"])
    auth_store.add_user(user)

    # 有 query 权限
    assert await check_permission(user, "datasource:mysql", "query") is True
    # 无 schema 权限
    assert await check_permission(user, "datasource:mysql", "schema") is False
    # 无 delete 权限
    assert await check_permission(user, "datasource:mysql", "delete") is False


# ============================================================
# 3. mask_columns 列脱敏
# ============================================================

def test_mask_columns_masks_string_values():
    """字符串脱敏：保留首尾各 1 字符，中间用 * 替换。"""
    user = User(
        id="u1", name="Test", tenant_id="default",
        masked_columns={"src1": ["ssn", "name"]},
    )
    columns = ["id", "name", "ssn", "age"]
    rows = [
        [1, "Alice", "123-45-6789", 30],
        [2, "Bob", "999-99-9999", 25],
    ]
    new_cols, new_rows = mask_columns(user, "src1", columns, rows)

    # 列名不变
    assert new_cols == columns
    # 非脱敏列不变
    assert new_rows[0][0] == 1
    assert new_rows[0][3] == 30
    # 脱敏列：保留首尾各 1 字符，中间替换为 *
    assert new_rows[0][1] == "A***e"          # "Alice" (5) -> A + *** + e
    assert new_rows[0][2] == "1*********9"    # "123-45-6789" (11) -> 1 + 9个* + 9
    assert new_rows[1][1] == "B*b"            # "Bob" (3) -> B + * + b
    assert new_rows[1][2] == "9*********9"    # "999-99-9999" (11) -> 9 + 9个* + 9


def test_mask_columns_short_string_full_replace():
    """长度 ≤ 2 的字符串全部替换为 *。"""
    user = User(
        id="u1", name="Test", tenant_id="default",
        masked_columns={"src1": ["code"]},
    )
    columns = ["code"]
    rows = [["ab"], ["a"], ["xyz"]]
    _, new_rows = mask_columns(user, "src1", columns, rows)

    # "ab" (len=2) -> "**"
    assert new_rows[0][0] == "**"
    # "a" (len=1) -> "*"
    assert new_rows[1][0] == "*"
    # "xyz" (len=3) -> "x*z"
    assert new_rows[2][0] == "x*z"


def test_mask_columns_non_string_not_masked():
    """非字符串值（数字、None、bool）不脱敏。"""
    user = User(
        id="u1", name="Test", tenant_id="default",
        masked_columns={"src1": ["val"]},
    )
    columns = ["val"]
    rows = [[123], [None], [True], [3.14]]
    _, new_rows = mask_columns(user, "src1", columns, rows)

    assert new_rows[0][0] == 123
    assert new_rows[1][0] is None
    assert new_rows[2][0] is True
    assert new_rows[3][0] == 3.14


def test_mask_columns_empty_string_not_masked():
    """空字符串不脱敏（isinstance(val, str) and val 条件）。"""
    user = User(
        id="u1", name="Test", tenant_id="default",
        masked_columns={"src1": ["name"]},
    )
    _, new_rows = mask_columns(user, "src1", ["name"], [[""]])
    assert new_rows[0][0] == ""


def test_mask_columns_no_masked_config_returns_original():
    """未配置脱敏列时返回原始数据。"""
    user = User(id="u1", name="Test", tenant_id="default")
    columns = ["a", "b"]
    rows = [[1, "secret"]]
    new_cols, new_rows = mask_columns(user, "src1", columns, rows)
    assert new_cols == columns
    assert new_rows == rows


def test_mask_columns_does_not_mutate_original_rows():
    """脱敏不应修改原始 rows。"""
    user = User(
        id="u1", name="Test", tenant_id="default",
        masked_columns={"src1": ["name"]},
    )
    original_rows = [["Alice"], ["Bob"]]
    _, new_rows = mask_columns(user, "src1", ["name"], [["Alice"], ["Bob"]])
    # 原始数据不受影响
    assert original_rows[0][0] == "Alice"
    assert original_rows[1][0] == "Bob"
    # 返回的是脱敏后的新数据
    assert new_rows[0][0] == "A***e"
    assert new_rows[1][0] == "B*b"
