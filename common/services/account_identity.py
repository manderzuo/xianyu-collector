"""从闲鱼登录态提取可展示的账号昵称。"""
from __future__ import annotations

from urllib.parse import unquote_plus


def _parse_cookie(cookie: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in str(cookie or "").split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key:
            values[key] = value
    return values


def extract_account_nickname(cookie: str | None) -> str:
    cookies = _parse_cookie(cookie or "")
    for key in ("tracknick", "nickname", "nick"):
        value = unquote_plus(str(cookies.get(key) or "")).strip()
        if value and not value.isdigit():
            return value[:64]
    return ""


def is_generated_account_name(name: str | None, goofish_id: str | None) -> bool:
    value = str(name or "").strip()
    platform_id = str(goofish_id or "").strip()
    return not value or value.isdigit() or value in {platform_id, f"闲鱼账号-{platform_id}"}


def display_account_name(account_name: str | None, goofish_id: str | None, cookie: str | None) -> str:
    nickname = extract_account_nickname(cookie)
    if nickname and is_generated_account_name(account_name, goofish_id):
        return nickname
    return str(account_name or nickname or goofish_id or "未命名账号")
