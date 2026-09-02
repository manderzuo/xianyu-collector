# -*- coding: utf-8 -*-
"""通用请求签名适配器。

这里只提供独立的参数规范化和 HMAC-SHA256 计算能力；平台具体字段由上层
业务传入，避免把任何外部项目的协议实现或固定密钥带入共享层。
"""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping


def canonical_query(params: Mapping[str, object]) -> str:
    """按键排序并生成稳定的 ``key=value`` 串。"""
    pairs = []
    for key in sorted(params):
        value = params[key]
        if value is None:
            continue
        pairs.append(f"{key}={value}")
    return "&".join(pairs)


def sign_request(secret: str, params: Mapping[str, object], body: str = "") -> str:
    """对规范化参数和请求体生成十六进制 HMAC 签名。"""
    message = f"{canonical_query(params)}{body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
