"""Small client for the shared Tencent Cloud registration service."""
from __future__ import annotations

import os
import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger("xr.cloud_auth")


class CloudAuthError(RuntimeError):
    """云端统一账号服务的可分类错误。"""

    def __init__(self, code: str, message: str, status_code: int = 503):
        self.code = str(code or "cloud_auth_error")
        self.status_code = int(status_code or 503)
        super().__init__(str(message or "云端账号服务请求失败"))


def cloud_auth_url() -> str:
    return os.getenv("XIANYU_CLOUD_AUTH_URL", "").strip().rstrip("/")


async def cloud_auth_request(action: str, payload: dict[str, Any], token: str = "") -> dict[str, Any] | None:
    base = cloud_auth_url()
    if not base:
        return None
    endpoint = f"{base}/api/xianyu/auth/{action}"
    response: httpx.Response | None = None
    last_error: httpx.HTTPError | None = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=8), follow_redirects=False) as client:
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                response = await client.post(endpoint, json=payload, headers=headers)
            break
        except httpx.HTTPError as exc:
            last_error = exc
            logger.warning(
                "cloud auth request failed action=%s attempt=%s error_type=%s error=%s",
                action,
                attempt,
                type(exc).__name__,
                str(exc)[:300],
            )
            if attempt < 3:
                await asyncio.sleep(0.5 * attempt)
    if response is None:
        message = "无法连接云端账号服务，请检查 Docker 网络或 DNS 设置"
        if isinstance(last_error, httpx.ConnectTimeout):
            message = "连接云端账号服务超时，请检查当前网络或代理设置"
        elif isinstance(last_error, httpx.ReadTimeout):
            message = "云端账号服务响应超时，请稍后重试"
        raise CloudAuthError("connection_failed", message, 503) from last_error
    try:
        body = response.json()
    except ValueError as exc:
        raise CloudAuthError(
            "invalid_response", "云端账号服务返回内容无效", 502
        ) from exc
    if not response.is_success or not isinstance(body, dict) or not body.get("ok"):
        detail = body.get("message") if isinstance(body, dict) else None
        code = body.get("code") if isinstance(body, dict) else None
        raise CloudAuthError(
            str(code or "cloud_auth_error"),
            str(detail or "云端账号服务拒绝了请求"),
            response.status_code if response.status_code >= 400 else 502,
        )
    return body


__all__ = ["CloudAuthError", "cloud_auth_request", "cloud_auth_url"]
