# -*- coding: utf-8 -*-
"""AI 回复配置与真实连接接口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.services.account_settings import load_platform_ai_settings, save_platform_ai_settings
from backend.app.services.entitlements import FEATURE_AI_SMART_REPLY, FEATURE_BUILTIN_AI_REPLY, require_feature
from common.db.session import get_session
from common.models.accounts import Account
from common.services.ai_provider_service import fetch_ai_model_list, test_ai_connection


router = APIRouter(prefix="/api/v1/ai-reply-settings", tags=["AI回复设置"])


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


async def _account(account_id: int, user: dict[str, Any], db: AsyncSession) -> Account:
    statement = select(Account).where(Account.id == account_id, Account.user_id == _uid(user))
    account = (await db.execute(statement)).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return account


async def _save_ai(account: Account, payload: dict[str, Any], db: AsyncSession) -> dict[str, Any]:
    current = await load_platform_ai_settings(db, int(account.user_id))
    current_ai = dict(current.get("ai_settings") or {})
    nested = payload.get("ai_settings")
    if isinstance(nested, dict):
        current_ai.update(nested)
    else:
        current_ai.update({key: value for key, value in payload.items() if key != "ai_settings"})
    values: dict[str, Any] = {"ai_settings": current_ai}
    if "ai_enabled" in payload:
        values["ai_enabled"] = bool(payload["ai_enabled"])
        current_ai["ai_enabled"] = values["ai_enabled"]
    elif "enabled" in payload:
        values["ai_enabled"] = bool(payload["enabled"])
        current_ai["ai_enabled"] = values["ai_enabled"]
    saved = await save_platform_ai_settings(db, int(account.user_id), values)
    return {**dict(saved.get("ai_settings") or {}), "ai_enabled": bool(saved.get("ai_enabled"))}


@router.post("/models")
async def models(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    await require_feature(db, user, FEATURE_AI_SMART_REPLY)
    data = payload or {}
    try:
        values = await fetch_ai_model_list(
            data.get("provider_type"), data.get("base_url"), data.get("api_key")
        )
        return ok({"models": values}, f"获取到 {len(values)} 个模型")
    except Exception as exc:
        return ok({"models": []}, f"获取模型列表失败：{str(exc)[:500]}")


@router.get("")
@router.get("/")
async def all_settings(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    await require_feature(db, user, FEATURE_AI_SMART_REPLY)
    settings = await load_platform_ai_settings(db, _uid(user))
    return ok(
        {**dict(settings.get("ai_settings") or {}), "ai_enabled": bool(settings.get("ai_enabled")), "builtin_ai_reply_enabled": bool(settings.get("builtin_ai_reply_enabled"))},
        "平台账号 AI 设置查询成功",
    )


@router.put("/builtin")
async def put_builtin_settings(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """开启或关闭 VIP 内置话术；同一平台账号下的闲鱼账号共享。"""
    await require_feature(db, user, FEATURE_BUILTIN_AI_REPLY)
    values = payload or {}
    current = await load_platform_ai_settings(db, _uid(user))
    enabled = bool(values.get("enabled", values.get("builtin_ai_reply_enabled", current.get("builtin_ai_reply_enabled"))))
    saved = await save_platform_ai_settings(db, _uid(user), {"builtin_ai_reply_enabled": enabled})
    return ok({"builtin_ai_reply_enabled": bool(saved.get("builtin_ai_reply_enabled"))}, f"内置AI自动回复已{'开启' if enabled else '关闭'}")


@router.post("")
@router.post("/")
@router.put("")
@router.put("/")
async def put_platform_settings(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """保存当前登录平台账号的 AI 配置，所属闲鱼账号统一共享。"""
    await require_feature(db, user, FEATURE_AI_SMART_REPLY)
    values = payload or {}
    current = await load_platform_ai_settings(db, _uid(user))
    current_ai = dict(current.get("ai_settings") or {})
    nested = values.get("ai_settings")
    if isinstance(nested, dict):
        current_ai.update(nested)
    else:
        current_ai.update({key: value for key, value in values.items() if key != "ai_enabled"})
    saved = await save_platform_ai_settings(
        db,
        _uid(user),
        {
            "ai_enabled": bool(values["ai_enabled"]) if "ai_enabled" in values else current.get("ai_enabled"),
            "ai_settings": current_ai,
        },
    )
    return ok(
        {**dict(saved.get("ai_settings") or {}), "ai_enabled": bool(saved.get("ai_enabled"))},
        "平台账号 AI 设置已保存，所属闲鱼账号共享",
    )


@router.post("/test")
async def test_platform_settings(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    await require_feature(db, user, FEATURE_AI_SMART_REPLY)
    settings = await load_platform_ai_settings(db, _uid(user))
    ai = settings.get("ai_settings") or {}
    try:
        reply = await test_ai_connection(ai.get("provider_type"), ai.get("base_url"), ai.get("api_key"), ai.get("model_name"))
        return ok({"tested": True, "reply": reply}, "AI连接测试成功")
    except Exception as exc:
        return ok({"tested": False}, f"AI连接测试失败：{str(exc)[:500]}")


@router.get("/{account_id}")
async def get_settings(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    await require_feature(db, user, FEATURE_AI_SMART_REPLY)
    account = await _account(account_id, user, db)
    settings = await load_platform_ai_settings(db, int(account.user_id))
    data = {**dict(settings.get("ai_settings") or {}), "ai_enabled": bool(settings.get("ai_enabled"))}
    return ok(data, "平台账号 AI 设置查询成功")


@router.post("/{account_id}")
@router.put("/{account_id}")
async def put_settings(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    await require_feature(db, user, FEATURE_AI_SMART_REPLY)
    account = await _account(account_id, user, db)
    return ok(await _save_ai(account, payload or {}, db), "平台账号 AI 设置已保存，所属闲鱼账号共享")


@router.post("/{account_id}/test")
async def test_settings(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    await require_feature(db, user, FEATURE_AI_SMART_REPLY)
    account = await _account(account_id, user, db)
    settings = await load_platform_ai_settings(db, int(account.user_id))
    ai = settings.get("ai_settings") or {}
    try:
        reply = await test_ai_connection(ai.get("provider_type"), ai.get("base_url"), ai.get("api_key"), ai.get("model_name"))
        return ok({"tested": True, "reply": reply}, "AI连接测试成功")
    except Exception as exc:
        return ok({"tested": False}, f"AI连接测试失败：{str(exc)[:500]}")
