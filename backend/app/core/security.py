# -*- coding: utf-8 -*-
"""不依赖第三方 JWT 库的最小 HS256 实现。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from common.config import settings


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, salt: str = "xr-default-salt") -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"pbkdf2${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, salt, expected = encoded.split("$", 2)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return hmac.compare_digest(actual, expected)


def create_access_token(user: dict[str, Any]) -> tuple[str, int]:
    return _create_token(user, settings.jwt_expire_minutes * 60)


def create_refresh_token(user: dict[str, Any]) -> tuple[str, int]:
    """创建独立的长期刷新令牌。

    刷新令牌不能复用短期 access token，否则 access token 过期后刷新接口
    也无法认证。令牌类型写入 payload，避免把普通 access token 当刷新令牌使用。
    """
    expires_in = max(settings.jwt_expire_minutes * 60, 30 * 24 * 60 * 60)
    return _create_token({**user, "token_type": "refresh"}, expires_in)


def _create_token(user: dict[str, Any], expires_in: int) -> tuple[str, int]:
    now = int(time.time())
    expires = now + expires_in
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps({**user, "iat": now, "exp": expires}, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = _b64(hmac.new(settings.jwt_secret.encode(), signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}", expires_in


def decode_access_token(token: str) -> dict[str, Any] | None:
    return _decode_token(token, expected_type=None)


def decode_refresh_token(token: str) -> dict[str, Any] | None:
    return _decode_token(token, expected_type="refresh")


def _decode_token(token: str, expected_type: str | None) -> dict[str, Any] | None:
    try:
        header, payload, signature = token.split(".", 2)
        expected = _b64(hmac.new(settings.jwt_secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(_unb64(payload))
        if int(data.get("exp", 0)) < int(time.time()):
            return None
        if expected_type is not None and data.get("token_type") != expected_type:
            return None
        if expected_type is None and data.get("token_type") == "refresh":
            return None
        return data
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None
