# -*- coding: utf-8 -*-
"""账号级配置的持久化辅助。

重写版的核心账号表只保存账号身份、Cookie 和生命周期状态；旧版账号列表
还包含一组账号级开关。这里将这些配置统一保存到 FeatureRecord，避免把旧
接口伪装成成功但刷新页面后丢失设置。
"""
from __future__ import annotations

import base64
import hashlib
from typing import Any, Iterable

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.feature_records import FeatureRecord
from common.config import settings as app_settings


ACCOUNT_SETTINGS_FEATURE = "account-settings"
PLATFORM_AI_SETTINGS_FEATURE = "platform-ai-settings"
PLATFORM_AI_SETTINGS_ID = "default"
PASSWORD_PREFIX = "enc:v1:"


def _password_fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(app_settings.jwt_secret.encode("utf-8")).digest())
    return Fernet(key)


def _encrypt_login_password(value: object) -> str:
    raw = str(value or "")
    if not raw or raw.startswith(PASSWORD_PREFIX):
        return raw
    return PASSWORD_PREFIX + _password_fernet().encrypt(raw.encode("utf-8")).decode("ascii")


def _decrypt_login_password(value: object) -> str:
    raw = str(value or "")
    if not raw.startswith(PASSWORD_PREFIX):
        # 兼容升级前的明文记录；下次保存账号设置时会自动加密。
        return raw
    try:
        return _password_fernet().decrypt(raw.removeprefix(PASSWORD_PREFIX).encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError):
        return ""


def _decrypt_settings(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return payload
    copied = dict(payload)
    copied["login_password"] = _decrypt_login_password(copied.get("login_password"))
    return copied


def default_account_settings() -> dict[str, Any]:
    return {
        "ai_enabled": False,
        "scheduled_redelivery": False,
        "scheduled_rate": False,
        "auto_polish": False,
        "auto_confirm": False,
        "confirm_before_send": False,
        "send_before_confirm": False,
        "only_send_card": False,
        "auto_red_flower": False,
        "ai_reply_block_ordered_users": False,
        "delivery_disabled": False,
        "delivery_disabled_reason": "",
        "pause_duration": 0,
        "message_expire_time": 0,
        "reply_delay_seconds": 0,
        "username": "",
        "login_password": "",
        "show_browser": False,
        "delivery_block_rules": [],
        "close_notice": False,
        "token_cache_cleared_at": None,
        "last_login_renew_requested_at": None,
        "ai_settings": {},
        "proxy_config": {"proxy_type": "none"},
        "refund_cancel": {"enabled": False, "url": "", "timeout": 30},
        "confirm_receipt": {"enabled": False, "message_content": "", "message_image": ""},
        "auto_rate": {"enabled": False, "rate_type": "text", "text_content": "", "api_url": ""},
        "default_reply": {
            "enabled": False,
            "reply_content": "",
            "reply_image": "",
            "reply_once": False,
            "reply_type": "text",
            "api_url": "",
            "api_timeout": 80,
        },
    }


def default_platform_ai_settings() -> dict[str, Any]:
    """平台账号级 AI 配置；同一平台账号下的闲鱼账号共享。"""
    return {
        "ai_enabled": False,
        "builtin_ai_reply_enabled": False,
        "ai_settings": {},
    }


def _copy_settings(payload: dict[str, Any] | None) -> dict[str, Any]:
    settings = default_account_settings()
    if isinstance(payload, dict):
        settings.update(payload)
    # 两种“确认顺序”本质上都是自动发货模式。旧版在开启其中任一项时，
    # 会要求自动确认发货能力同时开启；重写版此前只保存顺序开关，导致
    # “卡券发送成功再确认发货”被保存为 true，却在执行层被 auto_confirm
    # 再次拦截。这里统一归一化读写结果，兼容已经存在的不一致旧数据。
    if settings.get("only_send_card"):
        settings.update(
            auto_confirm=False,
            confirm_before_send=False,
            send_before_confirm=False,
        )
    elif settings.get("confirm_before_send"):
        settings.update(auto_confirm=True, send_before_confirm=False)
    elif settings.get("send_before_confirm"):
        settings.update(auto_confirm=True, confirm_before_send=False)
    return settings


def _copy_platform_ai_settings(payload: dict[str, Any] | None) -> dict[str, Any]:
    settings = default_platform_ai_settings()
    if isinstance(payload, dict):
        settings.update({key: value for key, value in payload.items() if key != "ai_settings"})
        nested = payload.get("ai_settings")
        if isinstance(nested, dict):
            settings["ai_settings"] = dict(nested)
    settings["ai_enabled"] = bool(settings.get("ai_enabled"))
    settings["builtin_ai_reply_enabled"] = bool(settings.get("builtin_ai_reply_enabled"))
    settings["ai_settings"] = dict(settings.get("ai_settings") or {})
    return settings


async def load_account_settings(
    db: AsyncSession,
    owner_id: int,
    account_id: int,
) -> dict[str, Any]:
    row = (
        await db.execute(
            select(FeatureRecord).where(
                FeatureRecord.owner_id == owner_id,
                FeatureRecord.feature == ACCOUNT_SETTINGS_FEATURE,
                FeatureRecord.external_id == str(account_id),
            )
        )
    ).scalar_one_or_none()
    return _copy_settings(_decrypt_settings(row.payload) if row else None)


async def load_account_settings_map(
    db: AsyncSession,
    owner_id: int,
    account_ids: Iterable[int],
) -> dict[int, dict[str, Any]]:
    ids = [str(account_id) for account_id in account_ids]
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(FeatureRecord).where(
                FeatureRecord.owner_id == owner_id,
                FeatureRecord.feature == ACCOUNT_SETTINGS_FEATURE,
                FeatureRecord.external_id.in_(ids),
            )
        )
    ).scalars().all()
    return {
        int(row.external_id): _copy_settings(_decrypt_settings(row.payload))
        for row in rows
        if row.external_id and row.external_id.isdigit()
    }


async def save_account_settings(
    db: AsyncSession,
    owner_id: int,
    account_id: int,
    values: dict[str, Any],
) -> dict[str, Any]:
    row = (
        await db.execute(
            select(FeatureRecord).where(
                FeatureRecord.owner_id == owner_id,
                FeatureRecord.feature == ACCOUNT_SETTINGS_FEATURE,
                FeatureRecord.external_id == str(account_id),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = FeatureRecord(
            owner_id=owner_id,
            feature=ACCOUNT_SETTINGS_FEATURE,
            external_id=str(account_id),
            status="active",
            payload=default_account_settings(),
            note="账号列表配置",
        )
        db.add(row)
    merged = {**_copy_settings(row.payload), **values}
    persisted = _copy_settings(merged)
    persisted["login_password"] = _encrypt_login_password(persisted.get("login_password"))
    row.payload = persisted
    await db.commit()
    await db.refresh(row)
    return _copy_settings(_decrypt_settings(row.payload))


async def load_platform_ai_settings(
    db: AsyncSession,
    owner_id: int,
) -> dict[str, Any]:
    """读取平台账号级 AI 配置，并兼容迁移旧的账号级配置。"""
    row = (
        await db.execute(
            select(FeatureRecord).where(
                FeatureRecord.owner_id == owner_id,
                FeatureRecord.feature == PLATFORM_AI_SETTINGS_FEATURE,
                FeatureRecord.external_id == PLATFORM_AI_SETTINGS_ID,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return _copy_platform_ai_settings(row.payload)

    # 老版本把 AI 配置保存在闲鱼账号下。首次读取时选取该平台账号已有的
    # 一份有效配置作为迁移兜底，但不修改旧记录，避免影响账号级历史数据。
    legacy_rows = (
        await db.execute(
            select(FeatureRecord)
            .where(
                FeatureRecord.owner_id == owner_id,
                FeatureRecord.feature == ACCOUNT_SETTINGS_FEATURE,
            )
            .order_by(FeatureRecord.id.desc())
        )
    ).scalars().all()
    for legacy in legacy_rows:
        payload = legacy.payload if isinstance(legacy.payload, dict) else {}
        ai_settings = payload.get("ai_settings")
        if isinstance(ai_settings, dict) and ai_settings:
            return _copy_platform_ai_settings(
                {"ai_enabled": payload.get("ai_enabled"), "ai_settings": ai_settings}
            )
    return default_platform_ai_settings()


async def save_platform_ai_settings(
    db: AsyncSession,
    owner_id: int,
    values: dict[str, Any],
) -> dict[str, Any]:
    """保存平台账号级 AI 配置，供该平台账号下所有闲鱼账号使用。"""
    row = (
        await db.execute(
            select(FeatureRecord).where(
                FeatureRecord.owner_id == owner_id,
                FeatureRecord.feature == PLATFORM_AI_SETTINGS_FEATURE,
                FeatureRecord.external_id == PLATFORM_AI_SETTINGS_ID,
            )
        )
    ).scalar_one_or_none()
    current = await load_platform_ai_settings(db, owner_id)
    if row is None:
        row = FeatureRecord(
            owner_id=owner_id,
            feature=PLATFORM_AI_SETTINGS_FEATURE,
            external_id=PLATFORM_AI_SETTINGS_ID,
            status="active",
            payload=current,
            note="平台账号共享 AI 配置",
        )
        db.add(row)
    merged = dict(current)
    if "ai_enabled" in values:
        merged["ai_enabled"] = bool(values["ai_enabled"])
    nested = values.get("ai_settings")
    if isinstance(nested, dict):
        merged["ai_settings"] = {**dict(current.get("ai_settings") or {}), **nested}
    row.payload = _copy_platform_ai_settings(merged)
    await db.commit()
    await db.refresh(row)
    return _copy_platform_ai_settings(row.payload)
