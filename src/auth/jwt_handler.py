"""JWT 签发与校验 —— 无状态认证令牌。"""

import os
import time
import json
import hmac
import hashlib
import base64
from typing import Optional
from fastapi import Depends, HTTPException, Header
from .store import get_auth_store


# 从环境变量读取密钥；生产环境必须配置强随机值
SECRET_KEY = os.getenv("JWT_SECRET", "datapilot-dev-secret-change-me")
ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = int(os.getenv("JWT_TTL", "3600"))  # 默认 1 小时


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_access_token(payload: dict, expires_in: int = None) -> str:
    """签发 JWT。

    payload 中应包含 user_id、tenant_id、roles；
    自动注入 iat 与 exp。
    """
    header = {"alg": ALGORITHM, "typ": "JWT"}
    now = int(time.time())
    body = {
        **payload,
        "iat": now,
        "exp": now + (expires_in or ACCESS_TOKEN_TTL),
    }
    header_b64 = _b64encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64encode(json.dumps(body, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}"
    signature = hmac.new(SECRET_KEY.encode(), signing_input.encode(), hashlib.sha256).digest()
    sig_b64 = _b64encode(signature)
    return f"{signing_input}.{sig_b64}"


def decode_access_token(token: str) -> Optional[dict]:
    """校验并解码 JWT；失败返回 None。

    校验项：
    - 三段式结构
    - header.alg 必须为 HS256（防算法混淆攻击，拒绝 "none" / "RS256" 等）
    - 签名常量时间比较
    - exp 过期校验
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts

        # 先解析 header，校验 alg —— 不验签前不信任任何字段
        header = json.loads(_b64decode(header_b64))
        if header.get("alg") != ALGORITHM:
            # 拒绝 alg=none / alg=RS256 等任何非预期算法
            return None
        if header.get("typ") and header["typ"].lower() != "jwt":
            return None

        signing_input = f"{header_b64}.{payload_b64}"
        expected_sig = hmac.new(SECRET_KEY.encode(), signing_input.encode(), hashlib.sha256).digest()
        actual_sig = _b64decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        payload = json.loads(_b64decode(payload_b64))
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


async def get_current_user(authorization: str = Header(None)) -> "User":
    """FastAPI 依赖：从 Authorization 头解析当前用户。

    用法：
        @router.post("/query")
        async def query(req: QueryRequest, user: User = Depends(get_current_user)):
            ...
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization[len("Bearer "):]
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing user_id")
    store = get_auth_store()
    user = await store.get_user(user_id, tenant_id=payload.get("tenant_id", ""))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user
