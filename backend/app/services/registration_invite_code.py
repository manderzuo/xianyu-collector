# -*- coding: utf-8 -*-
"""邀请码的可逆加密存储工具。

邀请码仍通过摘要校验注册有效性；加密副本只用于管理员在列表中复制已生成的码。
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from common.config import settings
from common.services.registration_invites import format_invite_code


def _fernet() -> Fernet:
    # Fernet 需要 32 字节 URL-safe key；使用部署级 JWT_SECRET 派生，避免新增明文密钥配置。
    material = f"xianyu-rewrite:registration-invite:{settings.jwt_secret}".encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
    return Fernet(key)


def encrypt_invite_code(value: str) -> str:
    """按展示格式加密邀请码，供数据库持久化。"""
    return _fernet().encrypt(format_invite_code(value).encode("utf-8")).decode("ascii")


def decrypt_invite_code(value: str | None) -> str | None:
    """解密邀请码；密钥变更或历史空值时返回 None。"""
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError):
        return None
