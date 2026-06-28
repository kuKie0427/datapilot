"""认证与权限模块 —— JWT 认证 + RBAC 角色权限模型 + 数据级过滤。"""

from .models import User, Role, Permission, UserRole
from .jwt_handler import create_access_token, decode_access_token, get_current_user
from .rbac import (
    RBACManager,
    require_permission,
    get_user_permissions,
    mask_columns,
)
from .store import InMemoryAuthStore, AuthStore

__all__ = [
    "User",
    "Role",
    "Permission",
    "UserRole",
    "create_access_token",
    "decode_access_token",
    "get_current_user",
    "RBACManager",
    "require_permission",
    "get_user_permissions",
    "mask_columns",
    "InMemoryAuthStore",
    "AuthStore",
]
