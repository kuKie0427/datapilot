"""权限存储 —— 内存实现 + 接口抽象，生产可替换为 DB 实现。"""

from typing import Optional
from .models import User, Role, Permission


class AuthStore:
    """权限存储接口抽象。"""

    async def get_user(self, user_id: str, tenant_id: str = "") -> Optional[User]:
        raise NotImplementedError

    async def get_role(self, name: str, tenant_id: str) -> Optional[Role]:
        raise NotImplementedError

    async def get_permission(self, permission_id: str) -> Optional[Permission]:
        raise NotImplementedError

    async def list_user_permissions(self, user: User) -> list[Permission]:
        """聚合用户所有角色下的权限点。"""
        result = []
        for role_name in user.roles:
            role = await self.get_role(role_name, user.tenant_id)
            if not role:
                continue
            for perm_id in role.permissions:
                perm = await self.get_permission(perm_id)
                if perm:
                    result.append(perm)
        return result


class InMemoryAuthStore(AuthStore):
    """内存存储 —— 用于开发与测试。

    生产环境应实现一个 DBAuthStore，把 user/role/permission 落库。
    """

    def __init__(self):
        self._users: dict[tuple[str, str], User] = {}
        self._roles: dict[tuple[str, str], Role] = {}
        self._permissions: dict[str, Permission] = {}

    def add_user(self, user: User):
        self._users[(user.id, user.tenant_id)] = user

    def add_role(self, role: Role):
        self._roles[(role.name, role.tenant_id)] = role

    def add_permission(self, perm: Permission):
        self._permissions[perm.id] = perm

    async def get_user(self, user_id: str, tenant_id: str = "") -> Optional[User]:
        return self._users.get((user_id, tenant_id))

    async def get_role(self, name: str, tenant_id: str) -> Optional[Role]:
        return self._roles.get((name, tenant_id))

    async def get_permission(self, permission_id: str) -> Optional[Permission]:
        return self._permissions.get(permission_id)


# 单例
_store: Optional[AuthStore] = None


def get_auth_store() -> AuthStore:
    global _store
    if _store is None:
        _store = _create_default_store()
    return _store


def set_auth_store(store: AuthStore):
    """用于测试或生产替换实现。"""
    global _store
    _store = store


def _create_default_store() -> InMemoryAuthStore:
    """构造默认的内存权限存储，预置三类角色与一个 admin 用户。"""
    store = InMemoryAuthStore()

    # 预置权限点
    store.add_permission(Permission(id="query:all", resource="datasource:*", action="query",
                                    description="查询所有数据源"))
    store.add_permission(Permission(id="schema:all", resource="datasource:*", action="schema",
                                    description="查看所有数据源 Schema"))
    store.add_permission(Permission(id="admin:all", resource="*", action="admin",
                                    description="系统管理"))

    # 预置角色
    store.add_role(Role(name="admin", tenant_id="default",
                        permissions=["query:all", "schema:all", "admin:all"],
                        description="管理员：全部权限"))
    store.add_role(Role(name="analyst", tenant_id="default",
                        permissions=["query:all", "schema:all"],
                        description="分析师：可查询可看 Schema"))
    store.add_role(Role(name="viewer", tenant_id="default",
                        permissions=["query:all"],
                        description="查看者：仅可查询"))

    # 预置一个 admin 用户用于开发联调
    # 密码哈希仅用于开发模式（DEBUG=true 且 ALLOW_NO_PASSWORD_LOGIN=true 时可不校验）
    # 生产部署应通过管理接口重置 password_hash，并禁用 dev-admin
    store.add_user(User(
        id="dev-admin",
        name="Dev Admin",
        tenant_id="default",
        roles=["admin"],
        password_hash="plain:dev-admin",  # 开发用明文，生产必须替换为 bcrypt 哈希
    ))
    return store
