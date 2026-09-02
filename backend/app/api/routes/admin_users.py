# -*- coding: utf-8 -*-
"""管理员用户管理真实接口。

旧版兼容层把 /admin/users 写入 FeatureRecord，导致页面看似保存但并不
产生可登录的系统用户。本路由直接操作 xr_users，并统一返回前端所需的
用户对象。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.core.security import hash_password
from common.db.session import get_session
from common.models import Account, User

router = APIRouter(prefix="/api/v1/admin/users", tags=["管理员用户管理"])

ROLE_TO_DB = {"ADMIN": "admin", "OPERATOR": "operator", "MEMBER": "user", "USER": "user"}
ROLE_TO_UI = {"admin": "ADMIN", "administrator": "ADMIN", "operator": "OPERATOR", "user": "MEMBER", "member": "MEMBER"}


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _require_admin(user: dict[str, Any]) -> int:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可以管理用户")
    try:
        return int(user.get("sub", 0))
    except (TypeError, ValueError):
        return 0


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="到期时间格式无效") from exc


def _parse_account_limit(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="账号数量限制必须是正整数") from exc
    if result <= 0:
        raise HTTPException(status_code=422, detail="账号数量限制必须是正整数")
    return result


def _db_role(value: Any) -> str:
    role = str(value or "MEMBER").strip().upper()
    if role not in ROLE_TO_DB:
        raise HTTPException(status_code=422, detail="用户角色无效")
    return ROLE_TO_DB[role]


def _db_status(value: Any, default: int = 1) -> int:
    if value in (None, ""):
        return default
    text_value = str(value).strip().upper()
    return 1 if text_value in {"1", "ACTIVE", "ENABLED", "正常"} else 0


async def _account_counts(db: AsyncSession, user_ids: list[int]) -> dict[int, int]:
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(Account.user_id, func.count(Account.id))
            .where(Account.user_id.in_(user_ids))
            .group_by(Account.user_id)
        )
    ).all()
    return {int(user_id): int(count) for user_id, count in rows}


def _serialize(user: User, account_count: int = 0) -> dict[str, Any]:
    role = str(user.role or "user").lower()
    balance = user.balance if user.balance is not None else Decimal("0")
    return {
        "id": user.id,
        "user_id": user.id,
        "username": user.username,
        "nickname": user.nickname,
        "email": user.email,
        "phone": user.phone,
        "role": ROLE_TO_UI.get(role, "MEMBER"),
        "status": "ACTIVE" if user.status else "INACTIVE",
        "is_admin": role in {"admin", "administrator"},
        "account_limit": user.account_limit,
        "balance": f"{Decimal(str(balance)):.2f}",
        "expire_at": user.expire_at.isoformat() if user.expire_at else None,
        "account_count": account_count,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
    }


def _apply_payload(user: User, payload: dict[str, Any], *, creating: bool) -> None:
    if creating:
        username = str(payload.get("username") or "").strip()
        if len(username) < 3:
            raise HTTPException(status_code=422, detail="用户名至少需要 3 个字符")
        password = str(payload.get("password") or "")
        if len(password) < 6:
            raise HTTPException(status_code=422, detail="密码至少需要 6 个字符")
        user.username = username
        user.password_hash = hash_password(password)
    elif "username" in payload:
        username = str(payload.get("username") or "").strip()
        if len(username) < 3:
            raise HTTPException(status_code=422, detail="用户名至少需要 3 个字符")
        user.username = username

    if "email" in payload:
        user.email = str(payload.get("email") or "").strip() or None
    if "phone" in payload:
        user.phone = str(payload.get("phone") or "").strip() or None
    if "role" in payload:
        user.role = _db_role(payload.get("role"))
    if "status" in payload:
        user.status = _db_status(payload.get("status"), user.status)
    if "account_limit" in payload:
        user.account_limit = _parse_account_limit(payload.get("account_limit"))
    if "expire_at" in payload:
        user.expire_at = _parse_datetime(payload.get("expire_at"))
    password = payload.get("password")
    if not creating and password:
        if len(str(password)) < 6:
            raise HTTPException(status_code=422, detail="密码至少需要 6 个字符")
        user.password_hash = hash_password(str(password))


@router.get("")
@router.get("/")
async def list_users(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    username: str | None = Query(None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    statement = select(User)
    count_statement = select(func.count()).select_from(User)
    if username and username.strip():
        term = f"%{username.strip()}%"
        statement = statement.where(User.username.like(term))
        count_statement = count_statement.where(User.username.like(term))
    rows = (await db.execute(statement.order_by(User.id.desc()).offset(offset).limit(limit))).scalars().all()
    counts = await _account_counts(db, [row.id for row in rows])
    total = int((await db.execute(count_statement)).scalar_one() or 0)
    return ok({
        "items": [_serialize(row, counts.get(row.id, 0)) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }, "用户查询成功")


@router.post("")
@router.post("/")
async def create_user(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    item = User(username="", password_hash="", role="user", status=1)
    _apply_payload(item, payload or {}, creating=True)
    db.add(item)
    try:
        await db.commit()
        await db.refresh(item)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="用户名已存在") from exc
    return ok({"user": _serialize(item)}, "用户创建成功")


@router.put("/{user_id}")
async def update_user(
    user_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    operator_id = _require_admin(user)
    item = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    values = payload or {}
    if user_id == operator_id and "status" in values and _db_status(values.get("status"), item.status) == 0:
        raise HTTPException(status_code=400, detail="不能停用当前登录管理员")
    _apply_payload(item, values, creating=False)
    try:
        await db.commit()
        await db.refresh(item)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="用户名已存在") from exc
    return ok({"user": _serialize(item)}, "用户更新成功")


@router.delete("/{user_id}")
async def disable_user(
    user_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    operator_id = _require_admin(user)
    if user_id == operator_id:
        raise HTTPException(status_code=400, detail="不能停用当前登录管理员")
    item = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    item.status = 0
    await db.commit()
    return ok({"user": _serialize(item), "disabled": True}, "用户已停用")


@router.post("/{user_id}/recharge")
async def recharge_user(
    user_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    item = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    try:
        amount = Decimal(str((payload or {}).get("amount", "0"))).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(status_code=422, detail="金额格式无效") from exc
    if amount == 0:
        raise HTTPException(status_code=422, detail="金额不能为 0")
    before = Decimal(str(item.balance or 0)).quantize(Decimal("0.01"))
    after = before + amount
    if after < 0:
        raise HTTPException(status_code=422, detail="扣减金额不能超过用户余额")
    item.balance = after
    await db.commit()
    return ok({
        "balance_before": f"{before:.2f}",
        "balance_after": f"{after:.2f}",
        "amount": f"{amount:.2f}",
        "user": _serialize(item),
    }, "用户余额已更新")
