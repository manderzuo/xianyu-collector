# -*- coding: utf-8 -*-
"""分销卡券上游代理。

旧版分销卡券页依赖一个独立的卡券服务。重写版保留同一组真实接口，
但把上游地址和管理密钥全部改为部署配置，并把用户的对接卡密保存到
当前工程的 FeatureRecord 中。
"""
from __future__ import annotations

import secrets
import string
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.config import settings
from common.models import FeatureRecord


CARD_SECRET_KEY_SETTING = "distribution.card_secret_key"
USER_SETTING_FEATURE = f"user-setting:{CARD_SECRET_KEY_SETTING}"
LEGACY_SETTING_FEATURE = f"legacy:user-settings/{CARD_SECRET_KEY_SETTING}"
AGENT_PREFIX = "/api/card-product/agent"
CREATE_KEY_PATH = "/api/api-management/keys/external-create-key"


def _fail(message: str, code: int | str = 400) -> dict[str, Any]:
    return {"success": False, "code": code, "message": message, "data": None}


def _setting_value(row: FeatureRecord | None) -> str:
    payload = row.payload if row and isinstance(row.payload, dict) else {}
    return str(payload.get("value") or "").strip()


async def get_card_secret_key(user_id: int, db: AsyncSession) -> str:
    rows = (
        await db.execute(
            select(FeatureRecord)
            .where(
                FeatureRecord.owner_id == user_id,
                FeatureRecord.feature.in_((USER_SETTING_FEATURE, LEGACY_SETTING_FEATURE)),
            )
            .order_by(FeatureRecord.id.desc())
        )
    ).scalars().all()
    return next((value for value in (_setting_value(row) for row in rows) if value), "")


def _base_url() -> str:
    return str(settings.card_dock_base_url or "").strip().rstrip("/")


async def request_upstream(
    user_id: int,
    db: AsyncSession,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_url = _base_url()
    if not base_url:
        return _fail("分销卡券上游地址未配置，请设置 CARD_DOCK_BASE_URL", "card_dock_not_configured")
    secret_key = await get_card_secret_key(user_id, db)
    if not secret_key:
        return _fail("尚未配置对接卡密秘钥，请前往个人设置-分销管理填写", "card_secret_missing")

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            if method.upper() == "GET":
                response = await client.get(
                    f"{base_url}{AGENT_PREFIX}{path}",
                    params={"api_key": secret_key, **(params or {})},
                )
            else:
                response = await client.post(
                    f"{base_url}{AGENT_PREFIX}{path}",
                    json={"api_key": secret_key, **(body or {})},
                )
    except httpx.HTTPError as exc:
        return _fail(f"调用上游卡券服务失败：{str(exc)[:300]}", "card_dock_upstream_unavailable")

    try:
        payload = response.json()
    except ValueError:
        return _fail(f"上游卡券服务返回非 JSON（HTTP {response.status_code}）", "card_dock_invalid_response")
    if not response.is_success:
        detail = payload.get("message") if isinstance(payload, dict) else None
        return _fail(str(detail or f"上游卡券服务 HTTP {response.status_code}")[:500], "card_dock_upstream_error")
    if not isinstance(payload, dict):
        return _fail("上游卡券服务返回格式不正确", "card_dock_invalid_response")
    return payload


def _random_key_name() -> str:
    alphabet = string.ascii_letters + string.digits
    return "xy_" + "".join(secrets.choice(alphabet) for _ in range(15))


async def create_card_secret_key(user_id: int, username: str, db: AsyncSession) -> dict[str, Any]:
    existing = await get_card_secret_key(user_id, db)
    if existing:
        return _fail("对接卡密秘钥已存在，如需重新创建请联系管理员重置", "card_secret_exists")
    base_url = _base_url()
    manager_key = str(settings.external_api_key or "").strip()
    if not base_url or not manager_key:
        return _fail("外部密钥服务未配置，请联系管理员设置 CARD_DOCK_BASE_URL 与 EXTERNAL_API_KEY", "card_secret_service_not_configured")
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            response = await client.post(
                f"{base_url}{CREATE_KEY_PATH}",
                json={
                    "key": manager_key,
                    "key_name": _random_key_name(),
                    "description": f"重写版系统用户 {username or user_id} 的分销卡券对接密钥",
                },
            )
    except httpx.HTTPError as exc:
        return _fail(f"调用外部密钥服务失败：{str(exc)[:300]}", "card_secret_service_unavailable")
    try:
        payload = response.json()
    except ValueError:
        return _fail("外部密钥服务返回非 JSON", "card_secret_invalid_response")
    payload_message = payload.get("message") if isinstance(payload, dict) else None
    if not response.is_success or not isinstance(payload, dict) or not payload.get("success"):
        return _fail(str(payload_message or f"外部密钥服务 HTTP {response.status_code}")[:500], "card_secret_create_failed")
    data = payload.get("data") or {}
    key_value = str(data.get("key_value") or data.get("key") or "").strip()
    if not key_value:
        return _fail("外部密钥服务未返回有效密钥", "card_secret_invalid_response")
    db.add(
        FeatureRecord(
            owner_id=user_id,
            feature=USER_SETTING_FEATURE,
            external_id=CARD_SECRET_KEY_SETTING,
            status="active",
            payload={"value": key_value, "description": "对接卡密秘钥"},
            note="分销卡券对接密钥",
        )
    )
    await db.commit()
    return {"success": True, "code": 200, "message": str(payload.get("message") or "对接卡密秘钥创建成功"), "data": {"key_value": key_value}}


def purchase_url(source_code: str, goods_id: int, sub_id: int, quantity: int, secret_key: str) -> str:
    query = urlencode({"api_key": secret_key, "source_code": source_code, "goods_id": goods_id, "sub_id": sub_id, "quantity": quantity})
    return f"{_base_url()}{AGENT_PREFIX}/purchase?{query}"


__all__ = [
    "CARD_SECRET_KEY_SETTING",
    "create_card_secret_key",
    "get_card_secret_key",
    "purchase_url",
    "request_upstream",
]
