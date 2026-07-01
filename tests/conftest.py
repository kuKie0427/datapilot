"""测试全局夹具与环境配置。

关键：必须在 import 任何 src.auth 模块之前设置 JWT_SECRET 环境变量，
因为 src/auth/jwt_handler.py 在模块加载时执行 SECRET_KEY = os.getenv("JWT_SECRET", ...)。
conftest.py 由 pytest 在收集测试模块前最先加载，因此在此处设置环境变量可保证
所有测试模块 import auth 时读到的 SECRET_KEY 一致。
"""

import os
import sys
import types
from pathlib import Path

# === 1. 在任何 src 导入前设置环境变量 ===
os.environ["JWT_SECRET"] = "test-secret-key-for-pytest-only-not-for-prod"
os.environ["DEBUG"] = "true"

# === 2. 构造轻量级 src 包 stub，避免执行 src/__init__.py ===
# src/__init__.py 会拉入 sentence_transformers / openai / mysql 等重依赖，
# 测试时只需导入具体子模块（auth / security / datasources），无需加载整个包。
# 通过在 sys.modules 中预置一个带 __path__ 的空模块，Python 不会再执行
# src/__init__.py，但仍能按 __path__ 查找子包。
_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if "src" not in sys.modules:
    _src_pkg = types.ModuleType("src")
    _src_pkg.__path__ = [_SRC_DIR]
    sys.modules["src"] = _src_pkg

import pytest

from src.auth.store import InMemoryAuthStore, set_auth_store, get_auth_store


@pytest.fixture
def auth_store():
    """提供干净的 InMemoryAuthStore 并注册为全局单例。

    每个测试用例获得独立的内存存储，避免相互污染；
    测试结束后恢复原单例。
    """
    store = InMemoryAuthStore()
    previous = get_auth_store()
    set_auth_store(store)
    yield store
    set_auth_store(previous)


class _MockLLM:
    """简易 mock LLM，可预设返回内容，用于需要 LLM 的测试。"""

    def __init__(self, response: str = "mock response"):
        self.response = response
        self.calls: list = []

    def invoke(self, prompt, **kwargs):
        self.calls.append(prompt)
        return self.response

    async def ainvoke(self, prompt, **kwargs):
        self.calls.append(prompt)
        return self.response


@pytest.fixture
def mock_llm():
    """提供 mock LLM 夹具。"""
    return _MockLLM()
