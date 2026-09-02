# -*- coding: utf-8 -*-
"""商品监控分类及兜底账号配置接口。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models import Account, FeatureRecord

category_router = APIRouter(prefix="/api/v1/product-monitor/categories", tags=["商品监控分类"])
collect_router = APIRouter(prefix="/api/v1/product-monitor/collect-fallback-accounts", tags=["兜底采集账号"])
order_router = APIRouter(prefix="/api/v1/product-monitor/order-fallback-accounts", tags=["兜底下单账号"])


def _uid(user: dict) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _admin(user: dict) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _category(row: FeatureRecord) -> dict[str, Any]:
    value = dict(row.payload or {})
    return {"id": row.id, "owner_id": row.owner_id, "name": str(value.get("name") or "未命名分类"), "is_deleted": row.status == "deleted", "created_at": row.created_at, "updated_at": row.updated_at}


@category_router.get("")
@category_router.get("/")
async def list_categories(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    statement = select(FeatureRecord).where(FeatureRecord.feature == "monitor-categories", FeatureRecord.status != "deleted")
    if not _admin(user): statement = statement.where(FeatureRecord.owner_id == _uid(user))
    rows = list((await db.execute(statement.order_by(FeatureRecord.id.asc()))).scalars().all())
    return ok([_category(row) for row in rows], "监控分类查询成功")


@category_router.post("")
@category_router.post("/")
async def create_category(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    name = str(payload.get("name") or payload.get("category_name") or "").strip()
    if not name: raise HTTPException(422, "分类名称不能为空")
    existing = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == "monitor-categories", FeatureRecord.status != "deleted"))).scalars().all())
    if any(str((row.payload or {}).get("name") or "").strip().casefold() == name.casefold() for row in existing):
        raise HTTPException(409, "分类名称已存在")
    row = FeatureRecord(owner_id=_uid(user), feature="monitor-categories", external_id=uuid4().hex, status="active", payload={"name": name}, note="监控分类")
    db.add(row); await db.commit(); await db.refresh(row)
    return ok(_category(row), "监控分类已创建")


async def _owned_category(category_id: int, user: dict, db: AsyncSession) -> FeatureRecord:
    statement = select(FeatureRecord).where(FeatureRecord.id == category_id, FeatureRecord.feature == "monitor-categories", FeatureRecord.status != "deleted")
    if not _admin(user): statement = statement.where(FeatureRecord.owner_id == _uid(user))
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None: raise HTTPException(404, "监控分类不存在")
    return row


@category_router.put("/{category_id}")
async def update_category(category_id: int, payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _owned_category(category_id, user, db)
    name = str(payload.get("name") or payload.get("category_name") or "").strip()
    if not name: raise HTTPException(422, "分类名称不能为空")
    row.payload = {**(row.payload or {}), "name": name}; await db.commit(); await db.refresh(row)
    return ok(_category(row), "监控分类已更新")


@category_router.delete("/{category_id}")
async def delete_category(category_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _owned_category(category_id, user, db)
    row.status = "deleted"; await db.commit()
    return ok({"id": category_id, "deleted": True}, "监控分类已删除")


def _category_value(value: Any) -> int | None:
    if value in (None, "", 0, "0"): return None
    try: return int(value)
    except (TypeError, ValueError): raise HTTPException(422, "分类ID无效")


async def _accounts(db: AsyncSession, owner_id: int, account_ids: list[str]) -> list[dict[str, Any]]:
    ids = [int(value) for value in account_ids if str(value).isdigit()]
    rows = list((await db.execute(select(Account).where(Account.user_id == owner_id, Account.id.in_(ids)))).scalars().all()) if ids else []
    by_id = {str(row.id): row for row in rows}
    return [{"account_id": value, "valid": value in by_id and bool((by_id[value].cookie or "").strip()), "reason": None if value in by_id and bool((by_id[value].cookie or "").strip()) else "账号不存在或没有有效Cookie"} for value in account_ids]


def _fallback(row: FeatureRecord | None, accounts: list[dict[str, Any]], category_name: str | None = None) -> dict[str, Any]:
    value = dict(row.payload or {}) if row else {}
    ids = [str(item) for item in value.get("account_ids") or []]
    return {"id": row.id if row else None, "owner_id": row.owner_id if row else None, "owner_username": value.get("owner_username"), "category_id": value.get("category_id"), "category_name": category_name or value.get("category_name"), "account_ids": ids, "accounts": accounts, "created_at": row.created_at if row else None, "updated_at": row.updated_at if row else None}


async def _fallback_rows(feature: str, user: dict, db: AsyncSession) -> list[FeatureRecord]:
    statement = select(FeatureRecord).where(FeatureRecord.feature == feature, FeatureRecord.status != "deleted")
    if not _admin(user): statement = statement.where(FeatureRecord.owner_id == _uid(user))
    return list((await db.execute(statement.order_by(FeatureRecord.id.desc()))).scalars().all())


async def _category_names(db: AsyncSession) -> dict[int, str]:
    rows = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == "monitor-categories", FeatureRecord.status != "deleted"))).scalars().all())
    return {row.id: str((row.payload or {}).get("name") or "") for row in rows}


def _build_fallback_router(router: APIRouter, feature: str, label: str) -> None:
    @router.get("")
    @router.get("/")
    async def list_fallback(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
        rows = await _fallback_rows(feature, user, db); names = await _category_names(db)
        return ok([_fallback(row, await _accounts(db, row.owner_id, [str(item) for item in (row.payload or {}).get("account_ids") or []]), names.get((row.payload or {}).get("category_id"))) for row in rows], f"{label}查询成功")

    @router.put("")
    @router.put("/")
    async def save_fallback(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
        target_owner = int(payload.get("owner_id") or _uid(user))
        if target_owner != _uid(user) and not _admin(user): raise HTTPException(403, "无权修改其他用户配置")
        category_id = _category_value(payload.get("category_id")); account_ids = [str(item) for item in payload.get("account_ids") or [] if str(item).strip()]
        names = await _category_names(db)
        rows = await _fallback_rows(feature, {"sub": str(target_owner), "role": "admin"}, db)
        row = next((item for item in rows if _category_value((item.payload or {}).get("category_id")) == category_id and item.owner_id == target_owner), None)
        value = {"category_id": category_id, "category_name": names.get(category_id), "account_ids": account_ids, "owner_username": payload.get("owner_username")}
        if row is None:
            row = FeatureRecord(owner_id=target_owner, feature=feature, external_id=uuid4().hex, status="active", payload=value, note=label); db.add(row)
        else: row.payload = {**(row.payload or {}), **value}; row.status = "active"
        await db.commit(); await db.refresh(row)
        return ok(_fallback(row, await _accounts(db, target_owner, account_ids), names.get(category_id)), f"{label}已保存")

    @router.delete("")
    @router.delete("/")
    async def delete_fallback(category_id: int | None = Query(default=None), owner_id: int | None = Query(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
        target_owner = int(owner_id or _uid(user))
        if target_owner != _uid(user) and not _admin(user): raise HTTPException(403, "无权删除其他用户配置")
        rows = await _fallback_rows(feature, {"sub": str(target_owner), "role": "admin"}, db)
        deleted = 0
        for row in rows:
            if row.owner_id == target_owner and _category_value((row.payload or {}).get("category_id")) == _category_value(category_id):
                row.status = "deleted"; deleted += 1
        await db.commit()
        return ok({"deleted": deleted, "category_id": category_id}, f"{label}已删除")


_build_fallback_router(collect_router, "collect-fallback-accounts", "兜底采集账号配置")
_build_fallback_router(order_router, "order-fallback-accounts", "兜底下单账号配置")
