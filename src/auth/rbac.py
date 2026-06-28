"""RBAC 核心逻辑 —— 权限校验、行级过滤、列脱敏。"""

from typing import Optional
from fastapi import HTTPException, Depends
from .models import User, Permission
from .store import get_auth_store
from .jwt_handler import get_current_user


async def get_user_permissions(user: User) -> list[Permission]:
    """获取用户聚合后的全部权限点。"""
    store = get_auth_store()
    return await store.list_user_permissions(user)


def _match_resource(pattern: str, resource: str) -> bool:
    """资源匹配：支持通配符 *。

    例：pattern="datasource:*" 匹配 resource="datasource:mysql_main"。
    """
    if pattern == "*":
        return True
    if pattern.endswith(":*"):
        prefix = pattern[:-2]
        return resource == prefix or resource.startswith(prefix + ":")
    return pattern == resource


async def check_permission(user: User, resource: str, action: str) -> bool:
    """检查用户是否对指定资源有指定动作的权限。"""
    permissions = await get_user_permissions(user)
    for perm in permissions:
        if _match_resource(perm.resource, resource) and perm.action == action:
            return True
        # admin 动作通配
        if perm.action == "admin" and _match_resource(perm.resource, resource):
            return True
    return False


def require_permission(resource: str, action: str):
    """FastAPI 依赖工厂：要求当前用户对 resource 有 action 权限。

    用法：
        @router.post("/sources/register")
        async def register_source(
            req: RegisterSourceRequest,
            user: User = Depends(require_permission("datasource:*", "admin")),
        ):
            ...
    注意：内部依赖 get_current_user 解析 token，调用方无需再单独声明 get_current_user。
    """
    async def _checker(user: User = Depends(get_current_user)):
        ok = await check_permission(user, resource, action)
        if not ok:
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: requires {action} on {resource}",
            )
        return user  # 返回 user 供路由继续使用

    return _checker


def mask_columns(user: User, source_id: str, columns: list[str], rows: list[list]) -> tuple[list[str], list[list]]:
    """对用户配置的脱敏列做掩码处理。

    只对字符串值脱敏，避免破坏数字、日期等类型；
    返回脱敏后的 (columns, rows)，原 rows 不被修改。
    """
    masked = user.masked_columns.get(source_id, [])
    if not masked:
        return columns, rows

    mask_idx = [i for i, c in enumerate(columns) if c in masked]
    if not mask_idx:
        return columns, rows

    new_rows = []
    for row in rows:
        new_row = list(row)
        for i in mask_idx:
            val = new_row[i]
            # 仅对字符串脱敏；数字、bool、datetime 等保持原值
            if isinstance(val, str) and val:
                s = val
                # 保留首尾各 1 个字符，其余用 * 替换
                if len(s) <= 2:
                    new_row[i] = "*" * len(s)
                else:
                    new_row[i] = s[0] + "*" * (len(s) - 2) + s[-1]
        new_rows.append(new_row)
    return columns, new_rows


class RBACManager:
    """RBAC 门面 —— 集中暴露权限相关操作。"""

    @staticmethod
    async def can_query(user: User, source_id: str) -> bool:
        return await check_permission(user, f"datasource:{source_id}", "query")

    @staticmethod
    async def can_view_schema(user: User, source_id: str) -> bool:
        return await check_permission(user, f"datasource:{source_id}", "schema")

    @staticmethod
    def row_filter_for(user: User, source_id: str) -> Optional[str]:
        return user.row_filters.get(source_id)

    @staticmethod
    def masked_columns_for(user: User, source_id: str) -> list[str]:
        return user.masked_columns.get(source_id, [])

    @staticmethod
    async def enforce_query(user: User, source_id: str):
        """查询前强制校验；不通过则抛 403。"""
        if not await RBACManager.can_query(user, source_id):
            raise HTTPException(
                status_code=403,
                detail=f"No query permission on datasource:{source_id}",
            )

    @staticmethod
    async def enforce_schema(user: User, source_id: str):
        if not await RBACManager.can_view_schema(user, source_id):
            raise HTTPException(
                status_code=403,
                detail=f"No schema permission on datasource:{source_id}",
            )
