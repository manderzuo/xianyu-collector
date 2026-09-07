"""Small client for the shared Tencent Cloud registration service."""
from __future__ import annotations

import os
from typing import Any

import httpx


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
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("云端账号服务暂时不可用，请稍后重试") from exc
    if not response.is_success or not isinstance(body, dict) or not body.get("ok"):
        detail = body.get("message") if isinstance(body, dict) else None
        raise RuntimeError(str(detail or "云端账号服务拒绝了请求"))
    return body
