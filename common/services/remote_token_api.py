# -*- coding: utf-8 -*-
"""远程闲鱼 Token 接口客户端。

远程服务的协议由重构版和源代码共同使用：通过 ``X-API-Key`` 认证，
请求体携带完整闲鱼 Cookie，并返回 Token 与设备 ID。该模块不保存 Cookie，
只负责一次请求和响应校验。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx


REMOTE_TOKEN_TYPE = "xianyu_token"
REMOTE_TOKEN_EMPTY_COOKIE_HINTS = (
    "cookies长度不足",
    "cookie长度不足",
    "cookies不能为空",
    "cookie不能为空",
    "未提供cookies",
    "未提供cookie",
    "需要cookies",
    "需要cookie",
)


@dataclass(frozen=True)
class RemoteTokenResult:
    success: bool
    message: str
    status_code: int = 0
    token: str = ""
    device_id: str = ""
    api_mode: str = ""
    response_json: Any = None


def validate_remote_token_settings(url: str, secret_key: str) -> str | None:
    clean_url = str(url or "").strip()
    clean_secret = str(secret_key or "").strip()
    if not clean_url:
        return "请选择远程接口时必须填写远程URL"
    if not clean_secret:
        return "请选择远程接口时必须填写秘钥"
    parsed = urlparse(clean_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "远程URL格式无效，请填写以 http:// 或 https:// 开头的完整地址"
    return None


def build_remote_token_payload(cookies: str = "") -> dict[str, Any]:
    return {"type": REMOTE_TOKEN_TYPE, "data": {"cookies": str(cookies or "")}}


def is_empty_cookie_validation_message(message: str) -> bool:
    normalized = str(message or "").replace(" ", "").replace("　", "").lower()
    return bool(normalized) and any(item in normalized for item in REMOTE_TOKEN_EMPTY_COOKIE_HINTS)


def parse_remote_token_response(response_json: Any, status_code: int) -> RemoteTokenResult:
    if not isinstance(response_json, dict):
        return RemoteTokenResult(False, "远程接口返回格式无效，响应不是JSON对象", status_code, response_json=response_json)
    message = str(response_json.get("message") or "")
    if not bool(response_json.get("success")):
        return RemoteTokenResult(False, message or "远程接口返回失败", status_code, response_json=response_json)
    data = response_json.get("data")
    if not isinstance(data, dict):
        return RemoteTokenResult(False, "远程接口返回成功但缺少data对象", status_code, response_json=response_json)
    token = str(data.get("token") or data.get("accessToken") or "").strip()
    device_id = str(data.get("device_id") or data.get("deviceId") or "").strip()
    if not token:
        return RemoteTokenResult(False, "远程接口返回成功但缺少token", status_code, response_json=response_json)
    if not device_id:
        return RemoteTokenResult(False, "远程接口返回成功但缺少device_id", status_code, response_json=response_json)
    return RemoteTokenResult(
        True,
        message or "远程接口取Token成功",
        status_code,
        token=token,
        device_id=device_id,
        api_mode=str(data.get("api_mode") or data.get("apiMode") or "").strip(),
        response_json=response_json,
    )


async def request_remote_xianyu_token(
    url: str,
    secret_key: str,
    *,
    cookies: str = "",
    timeout_seconds: float = 30,
) -> RemoteTokenResult:
    validation_error = validate_remote_token_settings(url, secret_key)
    if validation_error:
        return RemoteTokenResult(False, validation_error)
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
            response = await client.post(
                str(url).strip(),
                json=build_remote_token_payload(cookies),
                headers={"X-API-Key": str(secret_key).strip()},
            )
            try:
                response_json = response.json()
            except ValueError:
                return RemoteTokenResult(False, "远程接口返回内容不是有效JSON", response.status_code)
        return parse_remote_token_response(response_json, response.status_code)
    except httpx.TimeoutException:
        return RemoteTokenResult(False, "远程接口请求超时")
    except httpx.HTTPError as exc:
        return RemoteTokenResult(False, f"远程接口请求失败：{exc}")
