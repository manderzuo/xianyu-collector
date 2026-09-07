"""Small client for the shared Tencent Cloud registration service."""
from __future__ import annotations

import os
from typing import Any

import httpx


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
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            response = await client.post(f"{base}/api/xianyu/auth/{action}", json=payload, headers=headers)
        body = response.json()
    except httpx.HTTPError as exc:
        raise CloudAuthError(
            "connection_failed", "云端账号服务暂时不可用，请稍后重试", 503
        ) from exc
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
