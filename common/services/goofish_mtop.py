"""闲鱼网页 mtop 请求的独立封装。

发布、能力检测和后续平台动作都通过这里完成签名、Cookie 处理和错误分类。
它不读取浏览器隐式登录态，只使用账号表中明确传入的 Cookie。
"""
from __future__ import annotations

import hashlib
import json
import time
from http.cookies import SimpleCookie
from typing import Any

import httpx

from common.config import settings


APP_KEY = "34839810"
MTOP_TIMEOUT = httpx.Timeout(connect=15, read=45, write=30, pool=45)
TOKEN_MARKERS = (
    "FAIL_SYS_TOKEN_EXOIRED",
    "FAIL_SYS_TOKEN_EXPIRED",
    "FAIL_SYS_TOKEN_EMPTY",
)
SESSION_MARKERS = ("FAIL_SYS_SESSION_EXPIRED", "SESSION过期", "登录态已失效")
RISK_MARKERS = (
    "FAIL_SYS_USER_VALIDATE",
    "FAIL_SYS_ILLEGAL_ACCESS",
    "RGV587",
    "WUA_IS_MACHINE",
    "FAIL_BIZ_WUA_IS_MACHINE",
    "punish",
    "captcha",
    "validate",
    "挤爆",
)


def parse_cookie_string(value: str | None) -> dict[str, str]:
    """把 Cookie 字符串转换成可用于 httpx 的字典。"""
    cookies: dict[str, str] = {}
    for part in (value or "").split(";"):
        key, separator, item = part.strip().partition("=")
        if separator and key:
            cookies[key] = item
    return cookies


def serialize_cookies(cookies: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items() if key)


def _merge_response_cookies(original: str, response: httpx.Response) -> str:
    merged = parse_cookie_string(original)
    for header in response.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        parsed.load(header)
        for key, morsel in parsed.items():
            merged[key] = morsel.value
    return serialize_cookies(merged)


def _message(payload: dict[str, Any]) -> str:
    ret = payload.get("ret") or []
    if isinstance(ret, list) and ret:
        return str(ret[0])
    return "闲鱼接口未返回明确结果"


def _is_success(payload: dict[str, Any]) -> bool:
    ret = payload.get("ret") or []
    return bool(ret and any(str(value).startswith("SUCCESS") for value in ret))


async def mtop_call(
    *,
    api: str,
    data: dict[str, Any],
    cookies_str: str,
    account_id: str,
    proxy: str | None = None,
    origin: str = "https://www.goofish.com",
    referer: str = "https://www.goofish.com/",
    extra_params: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
    version: str = "1.0",
) -> dict[str, Any]:
    """调用 mtop，自动处理一次性令牌刷新并返回可展示的错误分类。"""
    current_cookie = cookies_str.strip()
    if not parse_cookie_string(current_cookie):
        return {
            "success": False,
            "account_invalid": True,
            "error": "账号 Cookie 为空，请先扫码登录",
            "cookies_str": current_cookie,
            "res": None,
        }

    last_error = ""
    for _attempt in range(3):
        cookie_map = parse_cookie_string(current_cookie)
        timestamp = str(int(time.time() * 1000))
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        token = cookie_map.get("_m_h5_tk", "").split("_", 1)[0]
        sign = hashlib.md5(f"{token}&{timestamp}&{APP_KEY}&{body}".encode()).hexdigest()
        params: dict[str, str] = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": timestamp,
            "sign": sign,
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": api,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.publish.0.0",
        }
        if extra_params:
            params.update(extra_params)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin,
            "Referer": referer,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Cookie": current_cookie,
        }
        if extra_headers:
            headers.update(extra_headers)
        try:
            async with httpx.AsyncClient(
                timeout=MTOP_TIMEOUT,
                follow_redirects=True,
                proxy=proxy or None,
            ) as client:
                response = await client.post(
                    f"{settings.goofish_mtop_host}/h5/{api}/{version}/",
                    params=params,
                    data={"data": body},
                    headers=headers,
                    cookies=cookie_map,
                )
                response.raise_for_status()
                payload = response.json()
                next_cookie = _merge_response_cookies(current_cookie, response)
        except (httpx.HTTPError, ValueError) as exc:
            last_error = f"闲鱼接口请求失败：{exc}"
            continue

        if not isinstance(payload, dict):
            last_error = "闲鱼接口返回格式异常"
            continue
        message = _message(payload)
        if _is_success(payload):
            return {
                "success": True,
                "account_invalid": False,
                "error": "",
                "cookies_str": next_cookie,
                "res": payload,
            }
        if any(marker in message for marker in TOKEN_MARKERS):
            if next_cookie != current_cookie:
                current_cookie = next_cookie
                continue
            last_error = message
            continue
        account_invalid = any(marker.lower() in message.lower() for marker in SESSION_MARKERS + RISK_MARKERS)
        return {
            "success": False,
            "account_invalid": account_invalid,
            "error": message,
            "cookies_str": next_cookie,
            "res": payload,
        }

    return {
        "success": False,
        "account_invalid": any(marker.lower() in last_error.lower() for marker in SESSION_MARKERS),
        "error": last_error or "闲鱼接口调用失败，请稍后重试",
        "cookies_str": current_cookie,
        "res": None,
    }


__all__ = ["mtop_call", "parse_cookie_string", "serialize_cookies"]
