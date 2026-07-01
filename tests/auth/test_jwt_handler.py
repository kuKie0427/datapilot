"""JWT 签发与校验测试 —— 覆盖算法混淆攻击、签名常量时间比较、
过期 token 拒绝、非三段式结构拒绝、正常 token 解码。
"""

import json
import time

import pytest

from src.auth.jwt_handler import (
    create_access_token,
    decode_access_token,
    _b64encode,
    _b64decode,
)


# ============================================================
# 1. 算法混淆攻击：alg=none / alg=RS256 的 token 被拒
# ============================================================

def test_algorithm_confusion_none_rejected():
    """alg=none 的 token 应被拒（算法混淆攻击）。"""
    header = {"alg": "none", "typ": "JWT"}
    payload = {"user_id": "u1", "exp": int(time.time()) + 3600}
    header_b64 = _b64encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    # alg=none 攻击：空签名
    token = f"{header_b64}.{payload_b64}."
    assert decode_access_token(token) is None


def test_algorithm_confusion_rs256_rejected():
    """alg=RS256 的 token 应被拒。"""
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"user_id": "u1", "exp": int(time.time()) + 3600}
    header_b64 = _b64encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    # 伪造的签名（非 HS256 签发）
    sig_b64 = _b64encode(b"forged-signature")
    token = f"{header_b64}.{payload_b64}.{sig_b64}"
    assert decode_access_token(token) is None


# ============================================================
# 2. 签名常量时间比较：篡改 payload 后签名不匹配
# ============================================================

def test_tampered_payload_signature_mismatch():
    """篡改 payload 后签名不匹配应被拒。"""
    token = create_access_token({"user_id": "u1", "roles": ["viewer"]})
    parts = token.split(".")
    header_b64, payload_b64, sig_b64 = parts

    # 解码 payload，篡改 user_id 后重新编码
    payload = json.loads(_b64decode(payload_b64))
    payload["user_id"] = "attacker"
    tampered_b64 = _b64encode(json.dumps(payload, separators=(",", ":")).encode())

    # 保留原签名 —— 签名是对原始 payload 计算的，篡改后不匹配
    tampered_token = f"{header_b64}.{tampered_b64}.{sig_b64}"
    assert decode_access_token(tampered_token) is None


def test_tampered_signature_rejected():
    """直接篡改签名字节应被拒。"""
    token = create_access_token({"user_id": "u1"})
    parts = token.split(".")
    # 翻转签名最后一个字符
    sig = parts[2]
    tampered_sig = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    assert decode_access_token(f"{parts[0]}.{parts[1]}.{tampered_sig}") is None


# ============================================================
# 3. 过期 token 被拒
# ============================================================

def test_expired_token_rejected():
    """过期 token 应被拒。"""
    # expires_in 为负数 → exp 已过去
    token = create_access_token({"user_id": "u1"}, expires_in=-10)
    assert decode_access_token(token) is None


# ============================================================
# 4. 非三段式结构被拒
# ============================================================

def test_non_three_part_structure_rejected():
    """非三段式结构的 token 应被拒。"""
    assert decode_access_token("") is None
    assert decode_access_token("abc") is None
    assert decode_access_token("a.b") is None
    assert decode_access_token("a.b.c.d") is None


def test_malformed_token_rejected():
    """格式错误的 token 应被拒。"""
    assert decode_access_token("not.a.valid-base64!") is None
    assert decode_access_token("...") is None


# ============================================================
# 5. 正常 token 解码成功
# ============================================================

def test_valid_token_decodes_successfully():
    """正常 token 解码成功，payload 字段完整。"""
    payload = {"user_id": "u1", "tenant_id": "t1", "roles": ["admin"]}
    token = create_access_token(payload)
    decoded = decode_access_token(token)

    assert decoded is not None
    assert decoded["user_id"] == "u1"
    assert decoded["tenant_id"] == "t1"
    assert decoded["roles"] == ["admin"]
    # 自动注入的时间字段
    assert "iat" in decoded
    assert "exp" in decoded
    assert decoded["exp"] > decoded["iat"]


def test_token_roundtrip_preserves_payload():
    """签发 → 解码往返应保持 payload 一致。"""
    original = {"user_id": "user-123", "tenant_id": "tenant-456", "roles": ["analyst"]}
    token = create_access_token(original)
    decoded = decode_access_token(token)
    assert decoded is not None
    for key, val in original.items():
        assert decoded[key] == val
