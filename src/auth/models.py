"""RBAC 数据模型 —— 用户、角色、权限及其关联。"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class User:
    """系统用户。

    tenant_id 用于多租户硬隔离；roles 为用户被授予的角色名列表。
    password_hash 存储密码哈希（bcrypt）；开发模式可用 "plain:" 前缀明文。
    """
    id: str
    name: str
    tenant_id: str
    email: Optional[str] = None
    roles: list[str] = field(default_factory=list)
    password_hash: str = ""  # bcrypt 哈希；空字符串表示未设置（禁止登录）
    # 数据级权限：行过滤条件（SQL 片段）与列脱敏名单
    row_filters: dict[str, str] = field(default_factory=dict)   # source_id -> "region = 'east'"
    masked_columns: dict[str, list[str]] = field(default_factory=dict)  # source_id -> [col, ...]


@dataclass
class Role:
    """角色 —— 按职能划分的权限集合。"""
    name: str
    tenant_id: str
    permissions: list[str] = field(default_factory=list)  # permission_id 列表
    description: str = ""


@dataclass
class Permission:
    """权限点。

    resource 形如 "datasource:hr_mysql"；action 形如 "query" / "schema" / "admin"。
    """
    id: str
    resource: str
    action: str
    description: str = ""


@dataclass
class UserRole:
    """用户-角色关联（多对多）。"""
    user_id: str
    role_name: str
    tenant_id: str
