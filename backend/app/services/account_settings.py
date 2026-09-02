# -*- coding: utf-8 -*-
"""账号级配置的持久化辅助。

重写版的核心账号表只保存账号身份、Cookie 和生命周期状态；旧版账号列表
还包含一组账号级开关。这里将这些配置统一保存到 FeatureRecord，避免把旧
接口伪装成成功但刷新页面后丢失设置。
"""
from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.feature_records import FeatureRecord


ACCOUNT_SETTINGS_FEATURE = "account-settings"


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


def _copy_settings(payload: dict[str, Any] | None) -> dict[str, Any]:
    settings = default_account_settings()
    if isinstance(payload, dict):
        settings.update(payload)
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
    return _copy_settings(row.payload if row else None)


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
        int(row.external_id): _copy_settings(row.payload)
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
    row.payload = {**_copy_settings(row.payload), **values}
    await db.commit()
    await db.refresh(row)
    return _copy_settings(row.payload)
