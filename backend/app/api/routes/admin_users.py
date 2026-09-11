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
from common.models import Account, Plan, User
from common.services.cloud_auth import CloudAuthError, cloud_auth_request, cloud_auth_url

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
    if text_value in {"2", "PENDING", "待审核"}:
        return 2
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
    status_value = int(user.status or 0)
    status_label = {1: "ACTIVE", 2: "PENDING"}.get(status_value, "INACTIVE")
    return {
        "id": user.id,
        "user_id": user.id,
        "username": user.username,
        "nickname": user.nickname,
        "email": user.email,
        "phone": user.phone,
        "role": ROLE_TO_UI.get(role, "MEMBER"),
        "status": status_label,
        "is_admin": role in {"admin", "administrator"},
        "cloud_mode": False,
        "account_limit": user.account_limit,
        "plan_code": user.plan_code or "NORMAL",
        "plan_expires_at": user.plan_expires_at.isoformat() if user.plan_expires_at else None,
        "balance": f"{Decimal(str(balance)):.2f}",
        "expire_at": user.expire_at.isoformat() if user.expire_at else None,
        "account_count": account_count,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
    }


def _serialize_remote_user(item: dict[str, Any]) -> dict[str, Any]:
    """Map a cloud approval user to the shape used by the local admin page."""
    status = str(item.get("status") or "").lower()
    plan_code = str(item.get("plan_code") or "NORMAL").strip().upper()
    plan_expires_at = item.get("plan_expires_at")
    return {
        "id": item.get("id"), "user_id": item.get("id"), "username": item.get("username"),
        "nickname": item.get("employee_name"), "email": None, "phone": None,
        "role": "ADMIN" if item.get("role") == "admin" else "MEMBER",
        "status": {"approved": "ACTIVE", "pending": "PENDING", "rejected": "INACTIVE", "disabled": "INACTIVE"}.get(status, "INACTIVE"),
        "is_admin": item.get("role") == "admin", "cloud_mode": True, "account_limit": None, "plan_code": plan_code,
        "plan_expires_at": plan_expires_at, "balance": "0.00", "expire_at": plan_expires_at, "account_count": 0,
        "created_at": item.get("created_at"), "updated_at": item.get("approved_at"),
    }


async def _list_remote_users(user: dict[str, Any], *, username: str | None, limit: int, offset: int) -> dict[str, Any] | None:
    if not cloud_auth_url():
        return None
    token = str(user.get("cloud_session_token") or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="云端管理员会话已失效，请退出后重新登录")
    try:
        remote = await cloud_auth_request("list_users", {}, token)
    except CloudAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    items = [_serialize_remote_user(item) for item in (remote or {}).get("items", [])]
    term = (username or "").strip().lower()
    if term:
        items = [item for item in items if term in str(item.get("username") or "").lower()]
    return {"items": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit}


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
        user.auth_version = int(user.auth_version or 1) + 1
    if "status" in payload:
        user.status = _db_status(payload.get("status"), user.status)
        user.auth_version = int(user.auth_version or 1) + 1
    if "account_limit" in payload:
        user.account_limit = _parse_account_limit(payload.get("account_limit"))
    if "expire_at" in payload:
        user.expire_at = _parse_datetime(payload.get("expire_at"))
    if "plan_code" in payload or "plan" in payload:
        plan_code = str(payload.get("plan_code") or payload.get("plan") or "NORMAL").strip().upper()
        if not plan_code or len(plan_code) > 32:
            raise HTTPException(status_code=422, detail="套餐编码无效")
        user.plan_code = plan_code
        user.auth_version = int(user.auth_version or 1) + 1
    if "plan_expires_at" in payload:
        user.plan_expires_at = _parse_datetime(payload.get("plan_expires_at"))
        user.auth_version = int(user.auth_version or 1) + 1
    password = payload.get("password")
    if not creating and password:
        if len(str(password)) < 6:
            raise HTTPException(status_code=422, detail="密码至少需要 6 个字符")
        user.password_hash = hash_password(str(password))
        user.auth_version = int(user.auth_version or 1) + 1


async def _validate_plan(db: AsyncSession, payload: dict[str, Any]) -> None:
    if "plan_code" not in payload and "plan" not in payload:
        return
    plan_code = str(payload.get("plan_code") or payload.get("plan") or "NORMAL").strip().upper()
    plan = (await db.execute(select(Plan).where(Plan.code == plan_code, Plan.status == "active"))).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=422, detail="套餐不存在或已停用")


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
    remote_items = await _list_remote_users(user, username=username, limit=limit, offset=offset)
    if remote_items is not None:
        return ok(remote_items, "用户查询成功")
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
    if cloud_auth_url():
        raise HTTPException(status_code=409, detail="云端模式请使用登录页的注册申请，不支持管理员在本机直接创建用户")
    values = payload or {}
    await _validate_plan(db, values)
    item = User(username="", password_hash="", role="user", plan_code="NORMAL", status=1)
    _apply_payload(item, values, creating=True)
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
    if cloud_auth_url():
        values = payload or {}
        allowed = {"plan", "plan_code", "plan_expires_at", "expire_at"}
        if not values or any(key not in allowed for key in values):
            raise HTTPException(
                status_code=409,
                detail="云端统一认证账号的资料由云端服务维护，本机不能修改；套餐与 VIP 开通请使用“套餐权限”页面",
            )
        token = str(user.get("cloud_session_token") or "").strip()
        if not token:
            raise HTTPException(status_code=401, detail="云端管理员会话已失效，请退出后重新登录")
        remote_payload: dict[str, Any] = {"user_id": user_id}
        if "plan_code" in values or "plan" in values:
            remote_payload["plan_code"] = values.get("plan_code") or values.get("plan")
        if "plan_expires_at" in values:
            remote_payload["plan_expires_at"] = values.get("plan_expires_at")
        elif "expire_at" in values:
            remote_payload["plan_expires_at"] = values.get("expire_at")
        try:
            remote = await cloud_auth_request("set_user_plan", remote_payload, token)
        except CloudAuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        remote_user = (remote or {}).get("user") or {}
        if not remote_user:
            remote_user = {
                "id": user_id,
                "username": str(values.get("username") or ""),
                "plan_code": (remote or {}).get("plan_code") or remote_payload.get("plan_code") or "NORMAL",
                "plan_expires_at": (remote or {}).get("plan_expires_at") if remote else remote_payload.get("plan_expires_at"),
            }
        return ok({"user": _serialize_remote_user(remote_user)}, "用户云端套餐已更新")
    item = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    values = payload or {}
    if user_id == operator_id and "status" in values and _db_status(values.get("status"), item.status) == 0:
        raise HTTPException(status_code=400, detail="不能停用当前登录管理员")
    await _validate_plan(db, values)
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
    if cloud_auth_url():
        token = str(user.get("cloud_session_token") or "").strip()
        if not token:
            raise HTTPException(status_code=401, detail="云端管理员会话已失效，请退出后重新登录")
        try:
            remote = await cloud_auth_request("disable_user", {"user_id": user_id}, token)
        except CloudAuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return ok({"user": _serialize_remote_user((remote or {}).get("user") or {}), "disabled": True}, "用户已停用")
    item = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    item.status = 0
    item.auth_version = int(item.auth_version or 1) + 1
    await db.commit()
    return ok({"user": _serialize(item), "disabled": True}, "用户已停用")


async def _get_pending_user(user_id: int, db: AsyncSession) -> User:
    item = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if int(item.status or 0) != 2:
        raise HTTPException(status_code=409, detail="该用户当前不是待审核状态")
    return item


@router.post("/{user_id}/approve")
async def approve_user(
    user_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    if cloud_auth_url():
        token = str(user.get("cloud_session_token") or "").strip()
        if not token:
            raise HTTPException(status_code=401, detail="云端管理员会话已失效，请退出后重新登录")
        try:
            remote = await cloud_auth_request("approve_user", {"user_id": user_id}, token)
        except CloudAuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return ok({"user": remote.get("user") if remote else {}, "approved": True}, "注册申请已通过")
    item = await _get_pending_user(user_id, db)
    item.status = 1
    item.auth_version = int(item.auth_version or 1) + 1
    await db.commit()
    await db.refresh(item)
    return ok({"user": _serialize(item), "approved": True}, "注册申请已通过")


@router.post("/{user_id}/reject")
async def reject_user(
    user_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    if cloud_auth_url():
        token = str(user.get("cloud_session_token") or "").strip()
        if not token:
            raise HTTPException(status_code=401, detail="云端管理员会话已失效，请退出后重新登录")
        try:
            remote = await cloud_auth_request("reject_user", {"user_id": user_id}, token)
        except CloudAuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return ok({"user": remote.get("user") if remote else {}, "rejected": True}, "注册申请已拒绝")
    item = await _get_pending_user(user_id, db)
    item.status = 0
    item.auth_version = int(item.auth_version or 1) + 1
    await db.commit()
    await db.refresh(item)
    return ok({"user": _serialize(item), "rejected": True}, "注册申请已拒绝")


@router.post("/{user_id}/recharge")
async def recharge_user(
    user_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    if cloud_auth_url():
        raise HTTPException(status_code=409, detail="云端模式暂不支持本机余额调整")
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
