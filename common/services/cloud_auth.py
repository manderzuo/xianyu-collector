"""Small client for the shared Tencent Cloud registration service."""
from __future__ import annotations

import os
import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("xr.cloud_auth")
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
DEFAULT_CLOUD_AUTH_ATTEMPTS = 5
DEFAULT_CLOUD_AUTH_CONNECT_TIMEOUT = 20.0
DEFAULT_CLOUD_AUTH_READ_TIMEOUT = 60.0
DEFAULT_CLOUD_AUTH_WRITE_TIMEOUT = 30.0
DEFAULT_CLOUD_AUTH_POOL_TIMEOUT = 20.0
DEFAULT_CLOUD_AUTH_RETRY_BACKOFF = 2.0
DEFAULT_CLOUD_AUTH_MAX_RETRY_BACKOFF = 10.0


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


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _cloud_auth_timeout(*, download: bool = False) -> httpx.Timeout:
    read_default = 90.0 if download else DEFAULT_CLOUD_AUTH_READ_TIMEOUT
    return httpx.Timeout(
        timeout=_env_float("XIANYU_CLOUD_AUTH_READ_TIMEOUT", read_default, 5.0, 300.0),
        connect=_env_float(
            "XIANYU_CLOUD_AUTH_CONNECT_TIMEOUT",
            DEFAULT_CLOUD_AUTH_CONNECT_TIMEOUT,
            5.0,
            120.0,
        ),
        write=_env_float(
            "XIANYU_CLOUD_AUTH_WRITE_TIMEOUT",
            DEFAULT_CLOUD_AUTH_WRITE_TIMEOUT,
            5.0,
            120.0,
        ),
        pool=_env_float(
            "XIANYU_CLOUD_AUTH_POOL_TIMEOUT",
            DEFAULT_CLOUD_AUTH_POOL_TIMEOUT,
            5.0,
            120.0,
        ),
    )


def _retry_delay(attempt: int) -> float:
    base = _env_float(
        "XIANYU_CLOUD_AUTH_RETRY_BACKOFF",
        DEFAULT_CLOUD_AUTH_RETRY_BACKOFF,
        0.5,
        30.0,
    )
    maximum = _env_float(
        "XIANYU_CLOUD_AUTH_MAX_RETRY_BACKOFF",
        DEFAULT_CLOUD_AUTH_MAX_RETRY_BACKOFF,
        1.0,
        60.0,
    )
    return min(maximum, base * (2 ** max(attempt - 1, 0)))


def _connection_error_message(error: httpx.HTTPError | None, *, download: bool = False) -> str:
    if isinstance(error, httpx.ConnectTimeout):
        return "连接云端账号服务超时，系统已多次等待，请检查当前网络或代理设置"
    if isinstance(error, httpx.ReadTimeout):
        return "云端账号服务响应较慢并超时，系统已多次等待，请稍后重试"
    if isinstance(error, httpx.ConnectError):
        return "连接云端账号服务异常，可能是网络或代理握手失败，请检查网络后重试"
    return "无法连接云端账号服务，系统已多次重试，请检查 Docker 网络或 DNS 设置"


async def _post_with_retries(
    endpoint: str,
    payload: dict[str, Any],
    token: str,
    action: str,
    *,
    download: bool = False,
) -> httpx.Response:
    attempts = _env_int("XIANYU_CLOUD_AUTH_MAX_ATTEMPTS", DEFAULT_CLOUD_AUTH_ATTEMPTS, 1, 8)
    timeout = _cloud_auth_timeout(download=download)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    last_error: httpx.HTTPError | None = None
    started = time.monotonic()

    for attempt in range(1, attempts + 1):
        attempt_started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.post(endpoint, json=payload, headers=headers)
            logger.info(
                "cloud auth response action=%s attempt=%s status=%s elapsed_ms=%s",
                action,
                attempt,
                response.status_code,
                int((time.monotonic() - attempt_started) * 1000),
            )
            return response
        except httpx.HTTPError as exc:
            last_error = exc
            logger.warning(
                "cloud auth request failed action=%s attempt=%s/%s elapsed_ms=%s error_type=%s error=%s",
                action,
                attempt,
                attempts,
                int((time.monotonic() - attempt_started) * 1000),
                type(exc).__name__,
                str(exc)[:300],
            )
            if attempt >= attempts:
                break
            delay = _retry_delay(attempt)
            logger.info(
                "cloud auth retry scheduled action=%s next_attempt=%s delay_seconds=%s elapsed_ms=%s",
                action,
                attempt + 1,
                delay,
                int((time.monotonic() - started) * 1000),
            )
            await asyncio.sleep(delay)

    raise CloudAuthError("connection_failed", _connection_error_message(last_error, download=download), 503) from last_error


async def cloud_auth_request(action: str, payload: dict[str, Any], token: str = "") -> dict[str, Any] | None:
    base = _validated_cloud_auth_url()
    if not base:
        return None
    endpoint = f"{base}/api/xianyu/auth/{action}"
    response = await _post_with_retries(endpoint, payload, token, action)
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
    response = await _post_with_retries(endpoint, payload, token, action, download=True)
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
