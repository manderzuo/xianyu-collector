# -*- coding: utf-8 -*-
"""管理员套餐、功能授权和单用户覆盖配置。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.services.entitlements import entitlement_payload, get_effective_entitlement, is_admin, user_id
from common.db.session import get_session
from common.models import EntitlementAuditLog, Plan, PlanEntitlement, User, UserEntitlementOverride

router = APIRouter(prefix="/api/v1/admin/entitlements", tags=["管理员套餐权限"])


def _require_admin(user: dict[str, Any]) -> int:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可以配置套餐权限")
    return user_id(user)


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="时间格式无效") from exc


def _plan_data(plan: Plan, entries: list[PlanEntitlement]) -> dict[str, Any]:
    return {
        "id": plan.id,
        "code": plan.code,
        "name": plan.name,
        "status": plan.status,
        "is_default": bool(plan.is_default),
        "features": {
            row.feature_key: {
                "enabled": bool(row.enabled),
                "limit": None if row.unlimited else row.limit_value,
                "unlimited": bool(row.unlimited),
                "config": row.config or {},
            }
            for row in entries
        },
    }


async def _audit(db: AsyncSession, actor_id: int, target_id: int | None, action: str, feature_key: str | None, payload: dict[str, Any]) -> None:
    db.add(EntitlementAuditLog(
        actor_user_id=actor_id,
        target_user_id=target_id,
        action=action,
        feature_key=feature_key,
        payload=payload,
    ))


@router.get("/plans")
async def list_plans(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _require_admin(user)
    plans = list((await db.execute(select(Plan).order_by(Plan.id.asc()))).scalars().all())
    result = []
    for plan in plans:
        entries = list((await db.execute(select(PlanEntitlement).where(PlanEntitlement.plan_id == plan.id))).scalars().all())
        result.append(_plan_data(plan, entries))
    return ok({"items": result, "total": len(result)}, "套餐查询成功")


@router.post("/plans")
async def create_plan(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    actor_id = _require_admin(user)
    code = str(payload.get("code") or "").strip().upper()
    name = str(payload.get("name") or code).strip()
    if not code or len(code) > 32 or not name:
        raise HTTPException(status_code=422, detail="套餐编码和名称不能为空")
    plan = Plan(code=code, name=name, status="active", is_default=bool(payload.get("is_default", False)))
    db.add(plan)
    try:
        await db.flush()
        await _audit(db, actor_id, None, "plan.create", None, {"code": code, "name": name})
        await db.commit()
        await db.refresh(plan)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="套餐编码已存在") from exc
    return ok(_plan_data(plan, []), "套餐创建成功")


@router.put("/plans/{plan_code}")
async def update_plan(plan_code: str, payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    actor_id = _require_admin(user)
    plan = (await db.execute(select(Plan).where(Plan.code == plan_code.strip().upper()))).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="套餐不存在")
    if "name" in payload:
        plan.name = str(payload.get("name") or plan.name).strip()
    if "status" in payload:
        plan.status = "active" if str(payload.get("status")).lower() in {"active", "enabled", "1"} else "inactive"
    await _audit(db, actor_id, None, "plan.update", None, payload)
    await db.commit()
    entries = list((await db.execute(select(PlanEntitlement).where(PlanEntitlement.plan_id == plan.id))).scalars().all())
    return ok(_plan_data(plan, entries), "套餐更新成功")


@router.put("/plans/{plan_code}/features/{feature_key:path}")
async def update_plan_feature(
    plan_code: str,
    feature_key: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    actor_id = _require_admin(user)
    plan = (await db.execute(select(Plan).where(Plan.code == plan_code.strip().upper()))).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="套餐不存在")
    row = (await db.execute(select(PlanEntitlement).where(PlanEntitlement.plan_id == plan.id, PlanEntitlement.feature_key == feature_key))).scalar_one_or_none()
    if row is None:
        row = PlanEntitlement(plan_id=plan.id, feature_key=feature_key, enabled=False, limit_value=0, unlimited=False)
        db.add(row)
    if "enabled" in payload:
        row.enabled = bool(payload.get("enabled"))
    if "limit" in payload or "limit_value" in payload:
        value = payload.get("limit", payload.get("limit_value"))
        if value is not None and int(value) < 0:
            raise HTTPException(status_code=422, detail="配额不能为负数")
        row.limit_value = int(value) if value is not None else None
    if "unlimited" in payload:
        row.unlimited = bool(payload.get("unlimited"))
    if "config" in payload and isinstance(payload.get("config"), dict):
        row.config = payload["config"]
    await _audit(db, actor_id, None, "plan.feature.update", feature_key, payload)
    await db.commit()
    await db.refresh(row)
    return ok({"feature_key": feature_key, "enabled": row.enabled, "limit": row.limit_value, "unlimited": row.unlimited, "config": row.config or {}}, "套餐功能已更新")


@router.get("/users/{target_user_id}")
async def get_user_entitlements(target_user_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _require_admin(user)
    target = (await db.execute(select(User).where(User.id == target_user_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    target_claims = {"sub": str(target.id), "role": target.role, "plan_code": target.plan_code, "auth_version": target.auth_version}
    overrides = list((await db.execute(select(UserEntitlementOverride).where(UserEntitlementOverride.user_id == target.id))).scalars().all())
    return ok({
        "user_id": target.id,
        "plan_code": target.plan_code or "NORMAL",
        "plan_expires_at": target.plan_expires_at.isoformat() if target.plan_expires_at else None,
        "overrides": [{
            "id": item.id, "feature_key": item.feature_key, "enabled": item.enabled,
            "limit": item.limit_value, "unlimited": item.unlimited,
            "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            "reason": item.reason,
        } for item in overrides],
        "effective": await entitlement_payload(db, target_claims),
    }, "用户权限查询成功")


@router.put("/users/{target_user_id}/plan")
async def set_user_plan(target_user_id: int, payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    actor_id = _require_admin(user)
    target = (await db.execute(select(User).where(User.id == target_user_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    code = str(payload.get("plan_code") or payload.get("plan") or "NORMAL").strip().upper()
    plan = (await db.execute(select(Plan).where(Plan.code == code, Plan.status == "active"))).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=422, detail="套餐不存在或已停用")
    target.plan_code = code
    target.plan_expires_at = _parse_datetime(payload.get("plan_expires_at")) if "plan_expires_at" in payload else target.plan_expires_at
    target.auth_version = int(target.auth_version or 1) + 1
    await _audit(db, actor_id, target.id, "user.plan.update", None, {"plan_code": code, "plan_expires_at": payload.get("plan_expires_at")})
    await db.commit()
    return ok({"user_id": target.id, "plan_code": target.plan_code, "plan_expires_at": target.plan_expires_at.isoformat() if target.plan_expires_at else None}, "用户套餐已更新")


@router.put("/users/{target_user_id}/features/{feature_key:path}")
async def set_user_feature(
    target_user_id: int,
    feature_key: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    actor_id = _require_admin(user)
    target = (await db.execute(select(User).where(User.id == target_user_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    row = (await db.execute(select(UserEntitlementOverride).where(UserEntitlementOverride.user_id == target.id, UserEntitlementOverride.feature_key == feature_key))).scalar_one_or_none()
    if row is None:
        row = UserEntitlementOverride(user_id=target.id, feature_key=feature_key, created_by=actor_id)
        db.add(row)
    if "enabled" in payload:
        row.enabled = bool(payload.get("enabled"))
    if "limit" in payload or "limit_value" in payload:
        value = payload.get("limit", payload.get("limit_value"))
        if value is not None and int(value) < 0:
            raise HTTPException(status_code=422, detail="配额不能为负数")
        row.limit_value = int(value) if value is not None else None
    if "unlimited" in payload:
        row.unlimited = bool(payload.get("unlimited"))
    if "expires_at" in payload:
        row.expires_at = _parse_datetime(payload.get("expires_at"))
    if "reason" in payload:
        row.reason = str(payload.get("reason") or "")[:255] or None
    target.auth_version = int(target.auth_version or 1) + 1
    await _audit(db, actor_id, target.id, "user.feature.update", feature_key, payload)
    await db.commit()
    await db.refresh(row)
    effective = await get_effective_entitlement(db, {"sub": str(target.id), "role": target.role, "plan_code": target.plan_code}, feature_key)
    return ok({"feature_key": feature_key, "override_id": row.id, "effective": effective.as_dict()}, "用户功能权限已更新")


@router.delete("/users/{target_user_id}/features/{feature_key:path}")
async def delete_user_feature(target_user_id: int, feature_key: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    actor_id = _require_admin(user)
    target = (await db.execute(select(User).where(User.id == target_user_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    await db.execute(delete(UserEntitlementOverride).where(UserEntitlementOverride.user_id == target.id, UserEntitlementOverride.feature_key == feature_key))
    target.auth_version = int(target.auth_version or 1) + 1
    await _audit(db, actor_id, target.id, "user.feature.delete", feature_key, {})
    await db.commit()
    return ok({"deleted": True, "feature_key": feature_key}, "用户功能覆盖已删除")
