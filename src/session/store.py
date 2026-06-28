"""会话存储 —— 内存实现，可替换为 Redis。

会话状态包括：
- 对话历史（最近 N 轮）
- 上次生成的 SQL（供增量修改）
- 上次查询的 source_id / db_id
- 抽取出的上下文槽位（时间范围、维度等）
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


SESSION_TTL = 30 * 60  # 30 分钟过期


@dataclass
class Session:
    """单个会话的状态。"""
    session_id: str
    user_id: str = ""
    tenant_id: str = ""
    messages: list[dict] = field(default_factory=list)  # [{"role":"user","content":"..."},...]
    last_sql: str = ""
    last_source_id: str = ""
    last_db_id: Optional[str] = None
    last_result_columns: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)  # 抽取的槽位（time_range, dimension 等）
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)

    def add_message(self, role: str, content: str):
        """添加一条对话消息，保留最近 10 轮。"""
        self.messages.append({"role": role, "content": content, "ts": time.time()})
        # 仅保留最近 20 条（10 轮）
        if len(self.messages) > 20:
            self.messages = self.messages[-20:]
        self.last_active_at = time.time()

    def is_expired(self, ttl: int = SESSION_TTL) -> bool:
        return time.time() - self.last_active_at > ttl


class SessionManager:
    """会话管理器 —— 内存实现。

    生产环境应替换为 Redis-backed 实现，原因：
    - 多 worker 时内存不共享
    - 服务重启会话丢失
    - 缺少分布式锁
    """

    def __init__(self, ttl: int = SESSION_TTL):
        self._sessions: dict[str, Session] = {}
        self._ttl = ttl

    def create_session(self, user_id: str = "", tenant_id: str = "") -> Session:
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        session = Session(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if not session:
            return None
        if session.is_expired(self._ttl):
            self._sessions.pop(session_id, None)
            return None
        return session

    def get_or_create(self, session_id: Optional[str], user_id: str = "") -> Session:
        """获取或创建会话。session_id 为空时创建新会话。"""
        if session_id:
            session = self.get_session(session_id)
            if session:
                return session
        return self.create_session(user_id=user_id)

    def update_after_query(
        self,
        session_id: str,
        question: str,
        sql: str,
        source_id: str,
        result_columns: list[str] = None,
        answer: str = "",
    ) -> Optional[Session]:
        """查询完成后更新会话状态。

        若会话已过期被清理，则重新创建一个并返回，保证会话连续性。
        """
        session = self.get_session(session_id)
        if not session:
            # 会话已过期 → 重新创建，避免丢失本轮查询结果
            session = self.create_session()
        session.add_message("user", question)
        session.add_message("assistant", answer or sql)
        session.last_sql = sql
        session.last_source_id = source_id
        session.last_result_columns = result_columns or []
        return session

    def cleanup_expired(self):
        """清理过期会话。"""
        expired = [sid for sid, s in self._sessions.items() if s.is_expired(self._ttl)]
        for sid in expired:
            self._sessions.pop(sid, None)


# 单例
_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    global _manager
    if _manager is None:
        _manager = SessionManager()
    return _manager
