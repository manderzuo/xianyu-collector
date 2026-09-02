# -*- coding: utf-8 -*-
"""当前用户资料及个人凭证接口。"""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Body, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import error
from backend.app.core.security import hash_password, verify_password
from common.db.session import get_session
from common.models import FeatureRecord, SystemSetting, User

router = APIRouter(prefix="/api/v1/users", tags=["当前用户"])


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _code(length: int = 8) -> str:
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(length))


def _secret() -> str:
    return secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:32]


async def _user(user: dict[str, Any], db: AsyncSession) -> User:
    record = (await db.execute(select(User).where(User.id == _uid(user)))).scalar_one_or_none()
    if record is None:
        raise ValueError("用户不存在")
    return record


async def _credential(kind: str, user_id: int, db: AsyncSession, *, reset: bool = False) -> str:
    feature = f"user-credential:{kind}"
    row = (
        await db.execute(
            select(FeatureRecord)
            .where(FeatureRecord.owner_id == user_id, FeatureRecord.feature == feature)
            .order_by(FeatureRecord.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    value = None if reset else (row.payload or {}).get("value") if row else None
    if not value:
        value = _code() if kind == "dock_code" else _secret()
    if row is None:
        row = FeatureRecord(owner_id=user_id, feature=feature, external_id=kind, status="active", payload={})
        db.add(row)
    row.payload = {**(row.payload or {}), "value": value}
    await db.commit()
    return str(value)


@router.get("/me")
async def get_current_user_profile(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    record = await _user(user, db)
    # 该接口保持旧版直出 UserPublic 的返回形状，个人设置页面直接读取 expire_at 等字段。
    return {
        "id": record.id,
        "username": record.username,
        "nickname": record.nickname,
        "email": record.email,
        "phone": record.phone,
        "role": record.role,
        "status": "ACTIVE" if record.status else "INACTIVE",
        "account_limit": record.account_limit,
        "balance": f"{Decimal(record.balance or 0):.2f}",
        "expire_at": record.expire_at.isoformat() if record.expire_at else None,
    }


@router.post("/change-password")
async def change_current_user_password(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    data = payload or {}
    current_password = str(data.get("current_password") or data.get("old_password") or "")
    new_password = str(data.get("new_password") or "")
    if len(new_password) < 6:
        return error("新密码长度不能少于6位", code="invalid_password")
    record = await _user(user, db)
    if not verify_password(current_password, record.password_hash):
        return error("当前密码不正确", code="invalid_password")
    record.password_hash = hash_password(new_password)
    await db.commit()
    return {"success": True, "code": "ok", "message": "密码修改成功", "data": None}


@router.post("/renew")
async def renew_current_user(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    data = payload or {}
    try:
        months = int(data.get("months", 0))
    except (TypeError, ValueError):
        months = 0
    if months < 1 or months > 120:
        return error("续期月数必须为1~120个月", code="invalid_months")
    price_row = (await db.execute(select(SystemSetting).where(SystemSetting.setting_key == "user.renew_month_price"))).scalar_one_or_none()
    try:
        unit_price = Decimal(str(price_row.setting_value if price_row else ""))
    except (InvalidOperation, ValueError):
        unit_price = Decimal("0")
    if unit_price <= 0:
        return error("续期功能未开放，请联系管理员配置续期单价", code="not_configured")
    record = await _user(user, db)
    total = unit_price * months
    balance = Decimal(record.balance or 0)
    if balance < total:
        return error(f"余额不足，续期 {months} 个月需 ¥{total:.2f}，当前余额 ¥{balance:.2f}", code="insufficient_balance")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    base = record.expire_at if record.expire_at and record.expire_at > now else now
    # 月份按 30 天折算，保持当前轻量部署不引入额外日期依赖。
    record.expire_at = base + timedelta(days=30 * months)
    record.balance = balance - total
    await db.commit()
    return {"success": True, "code": "ok", "message": "续期成功", "data": {
        "months": months,
        "unit_price": f"{unit_price:.2f}",
        "total": f"{total:.2f}",
        "balance_before": f"{balance:.2f}",
        "balance_after": f"{record.balance:.2f}",
        "expire_at": record.expire_at.isoformat(),
    }}


@router.get("/dock-code")
async def get_dock_code(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return {"success": True, "dock_code": await _credential("dock_code", _uid(user), db)}


@router.post("/dock-code/reset")
async def reset_dock_code(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return {"success": True, "code": "ok", "message": "对接码已重置", "data": {"dock_code": await _credential("dock_code", _uid(user), db, reset=True)}}


@router.get("/secret-key")
async def get_secret_key(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return {"success": True, "secret_key": await _credential("secret_key", _uid(user), db)}


@router.post("/secret-key/reset")
async def reset_secret_key(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    value = await _credential("secret_key", _uid(user), db, reset=True)
    return {"success": True, "code": "ok", "message": "分销秘钥已更换", "data": {"secret_key": value}}
