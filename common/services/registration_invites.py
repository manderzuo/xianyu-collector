"""注册邀请码的格式化和摘要工具。"""
from __future__ import annotations

import hashlib
import re


def normalize_invite_code(value: str) -> str:
    """允许用户复制带短横线/空格的邀请码，统一按大写纯字母数字处理。"""
    return re.sub(r"[\s-]+", "", str(value or "")).upper()


def hash_invite_code(value: str) -> str:
    return hashlib.sha256(normalize_invite_code(value).encode("utf-8")).hexdigest()


def format_invite_code(value: str) -> str:
    normalized = normalize_invite_code(value)
    return "-".join(normalized[index:index + 4] for index in range(0, len(normalized), 4))


def preview_invite_code(value: str) -> str:
    formatted = format_invite_code(value)
    groups = formatted.split("-")
    if len(groups) <= 2:
        return f"{groups[0]}-••••"
    return f"{groups[0]}-••••-••••-{groups[-1]}"
