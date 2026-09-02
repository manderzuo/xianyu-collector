# -*- coding: utf-8 -*-
"""AI 回复配置与真实连接接口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.services.account_settings import load_account_settings, save_account_settings
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
    statement = select(Account).where(Account.id == account_id)
    if not _is_admin(user):
        statement = statement.where(Account.user_id == _uid(user))
    account = (await db.execute(statement)).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return account


async def _save_ai(account: Account, payload: dict[str, Any], db: AsyncSession) -> dict[str, Any]:
    current = await load_account_settings(db, int(account.user_id), int(account.id))
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
    saved = await save_account_settings(db, int(account.user_id), int(account.id), values)
    return dict(saved.get("ai_settings") or {})


@router.post("/models")
async def models(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
):
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
    statement = select(Account).order_by(Account.id.asc())
    if not _is_admin(user):
        statement = statement.where(Account.user_id == _uid(user))
    accounts = (await db.execute(statement)).scalars().all()
    result: dict[str, Any] = {}
    for account in accounts:
        settings = await load_account_settings(db, int(account.user_id), int(account.id))
        result[str(account.id)] = dict(settings.get("ai_settings") or {})
        result[str(account.id)]["ai_enabled"] = bool(settings.get("ai_enabled"))
    return ok(result, "AI设置查询成功")


@router.get("/{account_id}")
async def get_settings(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    account = await _account(account_id, user, db)
    settings = await load_account_settings(db, int(account.user_id), account_id)
    data = dict(settings.get("ai_settings") or {})
    data["ai_enabled"] = bool(settings.get("ai_enabled"))
    return ok(data, "AI设置查询成功")


@router.post("/{account_id}")
@router.put("/{account_id}")
async def put_settings(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    account = await _account(account_id, user, db)
    return ok(await _save_ai(account, payload or {}, db), "AI设置已保存")


@router.post("/{account_id}/test")
async def test_settings(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    account = await _account(account_id, user, db)
    settings = await load_account_settings(db, int(account.user_id), account_id)
    ai = settings.get("ai_settings") or {}
    try:
        reply = await test_ai_connection(ai.get("provider_type"), ai.get("base_url"), ai.get("api_key"), ai.get("model_name"))
        return ok({"tested": True, "reply": reply}, "AI连接测试成功")
    except Exception as exc:
        return ok({"tested": False}, f"AI连接测试失败：{str(exc)[:500]}")
