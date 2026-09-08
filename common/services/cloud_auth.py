"""Small client for the shared Tencent Cloud registration service."""
from __future__ import annotations

import os
import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("xr.cloud_auth")
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024


class CloudAuthError(RuntimeError):
    """云端统一账号服务的可分类错误。"""

    def __init__(self, code: str, message: str, status_code: int = 503):
        self.code = str(code or "cloud_auth_error")
        self.status_code = int(status_code or 503)
        super().__init__(str(message or "云端账号服务请求失败"))


def cloud_auth_url() -> str:
    return os.getenv("XIANYU_CLOUD_AUTH_URL", "").strip().rstrip("/")


def _allow_insecure_cloud_auth() -> bool:
    return os.getenv("XIANYU_ALLOW_INSECURE_CLOUD_AUTH", "").strip().lower() in {"1", "true", "yes", "on"}


def _validated_cloud_auth_url() -> str:
    base = cloud_auth_url()
    if not base:
        return ""
    try:
        parsed = urlsplit(base)
    except ValueError as exc:
        raise CloudAuthError("insecure_configuration", "云端账号服务地址格式无效，请使用 HTTPS 地址", 503) from exc
    try:
        username = parsed.username
        password = parsed.password
        hostname = parsed.hostname
    except ValueError as exc:
        raise CloudAuthError("insecure_configuration", "云端账号服务地址格式无效，请使用 HTTPS 地址", 503) from exc
    if username or password or not hostname:
        raise CloudAuthError("insecure_configuration", "云端账号服务地址不安全，请使用不含凭据的 HTTPS 地址", 503)
    if parsed.scheme == "https":
        return base
    if parsed.scheme == "http" and _allow_insecure_cloud_auth():
        logger.warning("cloud auth is using explicitly enabled insecure HTTP endpoint host=%s", hostname)
        return base
    raise CloudAuthError("insecure_configuration", "云端账号服务必须使用 HTTPS，请检查 XIANYU_CLOUD_AUTH_URL", 503)


async def cloud_auth_request(action: str, payload: dict[str, Any], token: str = "") -> dict[str, Any] | None:
    base = _validated_cloud_auth_url()
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


async def cloud_auth_download(action: str, payload: dict[str, Any], token: str = "") -> tuple[bytes, str]:
    """Download a protected binary from the shared cloud service."""
    base = _validated_cloud_auth_url()
    if not base:
        raise CloudAuthError("not_configured", "云端账号服务未配置", 503)
    endpoint = f"{base}/api/xianyu/auth/{action}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=8), follow_redirects=False) as client:
            response = await client.post(endpoint, json=payload, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise CloudAuthError("connection_failed", "无法连接云端账号服务，请稍后重试", 503) from exc
    content_type = response.headers.get("content-type", "")
    if not response.is_success or "application/zip" not in content_type:
        try:
            body = response.json()
        except ValueError:
            body = {}
        raise CloudAuthError(str(body.get("code") or "cloud_auth_error"), str(body.get("message") or "云端日志下载失败"), response.status_code if response.status_code >= 400 else 502)
    content_length = response.headers.get("content-length", "").strip()
    if content_length:
        try:
            if int(content_length) > MAX_DOWNLOAD_BYTES:
                raise CloudAuthError("invalid_response", "云端日志响应超过大小限制", 502)
        except ValueError as exc:
            raise CloudAuthError("invalid_response", "云端日志响应大小无效", 502) from exc
    data = response.content
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise CloudAuthError("invalid_response", "云端日志响应超过大小限制", 502)
    disposition = response.headers.get("content-disposition", "")
    filename = "diagnostic-report.zip"
    marker = "filename*=UTF-8''"
    if marker in disposition:
        filename = disposition.split(marker, 1)[1].strip().strip('"') or filename
    return data, filename


__all__ = ["CloudAuthError", "cloud_auth_download", "cloud_auth_request", "cloud_auth_url"]
