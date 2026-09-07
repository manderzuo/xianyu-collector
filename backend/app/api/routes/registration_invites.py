# -*- coding: utf-8 -*-
"""管理员注册邀请码管理接口。"""
from __future__ import annotations

import secrets
import string
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.services.registration_invite_code import decrypt_invite_code, encrypt_invite_code
from common.db.session import get_session
from common.models import RegistrationInvite
from common.services.registration_invites import (
    format_invite_code,
    hash_invite_code,
    preview_invite_code,
)

router = APIRouter(prefix="/api/v1/admin/invites", tags=["管理员邀请码"])
CODE_ALPHABET = string.ascii_uppercase + string.digits


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _require_admin(user: dict[str, Any]) -> int:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可以管理注册邀请码")
    try:
        return int(user.get("sub", 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="管理员身份无效") from exc


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="过期时间格式无效") from exc


def _effective_status(item: RegistrationInvite, now: datetime | None = None) -> str:
    current = now or datetime.now()
    if item.status == "active" and item.expires_at and item.expires_at <= current:
        return "expired"
    return item.status


def _serialize(item: RegistrationInvite, now: datetime | None = None) -> dict[str, Any]:
    full_code = decrypt_invite_code(item.code_encrypted)
    return {
        "id": item.id,
        "code": full_code or item.code_preview,
        "code_preview": item.code_preview,
        "code_available": bool(full_code),
        "status": _effective_status(item, now),
        "note": item.note,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "used_at": item.used_at.isoformat() if item.used_at else None,
        "used_by": item.used_by,
        "created_by": item.created_by,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def _new_raw_code() -> str:
    # 16 位随机字母数字，足够避免可猜测和碰撞；页面展示为 4-4-4-4。
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(16))


@router.get("")
@router.get("/")
async def list_invites(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    statement = select(RegistrationInvite)
    count_statement = select(func.count()).select_from(RegistrationInvite)
    status_value = (status or "").strip().lower()
    if status_value in {"active", "used", "revoked", "expired"}:
        if status_value == "expired":
            # 过期状态同时兼容已落库与尚未被请求触发的 active 记录。
            now = datetime.now()
            statement = statement.where(
                (RegistrationInvite.status == "expired")
                | ((RegistrationInvite.status == "active") & (RegistrationInvite.expires_at <= now))
            )
            count_statement = count_statement.where(
                (RegistrationInvite.status == "expired")
                | ((RegistrationInvite.status == "active") & (RegistrationInvite.expires_at <= now))
            )
        else:
            statement = statement.where(RegistrationInvite.status == status_value)
            count_statement = count_statement.where(RegistrationInvite.status == status_value)
    rows = (
        await db.execute(statement.order_by(RegistrationInvite.id.desc()).offset(offset).limit(limit))
    ).scalars().all()
    total = int((await db.execute(count_statement)).scalar_one() or 0)
    return ok({
        "items": [_serialize(item) for item in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }, "邀请码查询成功")


@router.post("")
@router.post("/")
async def create_invites(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    operator_id = _require_admin(user)
    values = payload or {}
    try:
        count = int(values.get("count", 1))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="生成数量必须是 1-100 的整数") from exc
    if count < 1 or count > 100:
        raise HTTPException(status_code=422, detail="生成数量必须是 1-100 的整数")
    expires_at = _parse_datetime(values.get("expires_at"))
    if expires_at and expires_at <= datetime.now():
        raise HTTPException(status_code=422, detail="过期时间必须晚于当前时间")
    note = str(values.get("note") or "").strip()[:255] or None

    created: list[dict[str, Any]] = []
    for _ in range(count):
        raw_code = _new_raw_code()
        item = RegistrationInvite(
            code_hash=hash_invite_code(raw_code),
            code_preview=preview_invite_code(raw_code),
            code_encrypted=encrypt_invite_code(raw_code),
            created_by=operator_id,
            status="active",
            note=note,
            expires_at=expires_at,
        )
        db.add(item)
        created.append({"code": format_invite_code(raw_code), "item": item})
    try:
        await db.commit()
        for entry in created:
            await db.refresh(entry["item"])
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail="邀请码生成失败，请重试") from exc

    return ok({
        "items": [
            {**_serialize(entry["item"]), "code": entry["code"]}
            for entry in created
        ],
        "codes": [entry["code"] for entry in created],
        "count": len(created),
    }, f"已生成 {len(created)} 个邀请码")


@router.post("/{invite_id}/revoke")
async def revoke_invite(
    invite_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    item = (
        await db.execute(select(RegistrationInvite).where(RegistrationInvite.id == invite_id))
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    if _effective_status(item) != "active":
        raise HTTPException(status_code=400, detail="只有未使用且未过期的邀请码可以撤销")
    item.status = "revoked"
    await db.commit()
    await db.refresh(item)
    return ok({"item": _serialize(item)}, "邀请码已撤销")
