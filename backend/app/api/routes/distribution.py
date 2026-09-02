# -*- coding: utf-8 -*-
"""分销链路的持久化实现。

当前重写版的卡券统一保存在 ``FeatureRecord(feature='cards')`` 中。这里将旧版
分销接口迁移到同一套记录存储，保留原有权限、一级/二级对接、提货和流水语义，
避免页面请求落入“等待连接”的通用兼容路由。
"""
from __future__ import annotations

import json
import secrets
import string
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.config import settings
from common.db.session import get_session
from common.models import FeatureRecord, Order, SystemSetting, User

router = APIRouter(prefix="/api/v1/distribution", tags=["分销管理"])

CARD_FEATURE = "cards"
DOCK_FEATURE = "distribution-dock-records"
BINDING_FEATURE = "distribution-source-bindings"
AGENT_FEATURE = "distribution-agent-orders"
FLOW_FEATURE = "distribution-fund-flows"
CREDENTIAL_PREFIX = "user-credential:"


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "active", "enabled"}


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def _payload(row: FeatureRecord) -> dict[str, Any]:
    return dict(row.payload or {}) if isinstance(row.payload, dict) else {}


def _page(items: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
    total = len(items)
    total_pages = (total + page_size - 1) // page_size if total else 0
    return {"list": items[(page - 1) * page_size: page * page_size], "total": total, "page": page, "page_size": page_size, "total_pages": total_pages}


async def _users(db: AsyncSession) -> dict[int, User]:
    return {int(item.id): item for item in (await db.execute(select(User))).scalars().all()}


async def _records(db: AsyncSession, feature: str, owner_id: int | None = None) -> list[FeatureRecord]:
    statement = select(FeatureRecord).where(FeatureRecord.feature == feature)
    if owner_id is not None:
        statement = statement.where(FeatureRecord.owner_id == owner_id)
    return list((await db.execute(statement.order_by(FeatureRecord.id.desc()))).scalars().all())


def _public_card(row: FeatureRecord, current_id: int, docked: dict[int, FeatureRecord]) -> dict[str, Any]:
    data = _payload(row)
    dock = docked.get(row.id)
    return {
        "id": row.id,
        "user_id": row.owner_id,
        "name": str(data.get("name") or "未命名卡券"),
        "type": str(data.get("type") or "text"),
        "description": data.get("description"),
        "price": str(data.get("price") or "0"),
        "fee_payer": data.get("fee_payer"),
        "min_price": str(data.get("min_price") or "0"),
        "is_multi_spec": _bool(data.get("is_multi_spec")),
        "spec_name": data.get("spec_name"),
        "spec_value": data.get("spec_value"),
        "is_docked": dock is not None,
        "dock_record_id": dock.id if dock else None,
        "created_at": _iso(row.created_at),
    }


def _card_is_enabled(row: FeatureRecord) -> bool:
    data = _payload(row)
    return row.status not in {"disabled", "inactive", "deleted"} and _bool(data.get("enabled"), True)


async def _credential(db: AsyncSession, owner_id: int, kind: str, *, create: bool = False) -> str | None:
    row = (
        await db.execute(
            select(FeatureRecord)
            .where(FeatureRecord.owner_id == owner_id, FeatureRecord.feature == f"{CREDENTIAL_PREFIX}{kind}")
            .order_by(FeatureRecord.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    value = _payload(row).get("value") if row else None
    if value or not create:
        return str(value) if value else None
    alphabet = string.ascii_uppercase + string.digits
    value = "".join(secrets.choice(alphabet) for _ in range(8)) if kind == "dock_code" else secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:32]
    if row is None:
        row = FeatureRecord(owner_id=owner_id, feature=f"{CREDENTIAL_PREFIX}{kind}", external_id=kind, status="active", payload={})
        db.add(row)
    row.payload = {**_payload(row), "value": value}
    await db.commit()
    return value


async def _card(db: AsyncSession, card_id: int) -> FeatureRecord | None:
    return (await db.execute(select(FeatureRecord).where(FeatureRecord.id == card_id, FeatureRecord.feature == CARD_FEATURE))).scalar_one_or_none()


async def _dock(db: AsyncSession, dock_id: int) -> FeatureRecord | None:
    return (await db.execute(select(FeatureRecord).where(FeatureRecord.id == dock_id, FeatureRecord.feature == DOCK_FEATURE))).scalar_one_or_none()


def _dock_data(row: FeatureRecord) -> dict[str, Any]:
    data = _payload(row)
    return {**data, "id": row.id, "user_id": row.owner_id, "status": _bool(data.get("status"), row.status == "active"), "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at)}


async def _dock_view(row: FeatureRecord, cards: dict[int, FeatureRecord], users: dict[int, User], all_docks: dict[int, FeatureRecord]) -> dict[str, Any]:
    data = _dock_data(row)
    card = cards.get(int(data.get("card_id") or 0))
    card_data = _payload(card) if card else {}
    owner_id = int(data.get("card_owner_id") or (card.owner_id if card else 0))
    parent = all_docks.get(int(data.get("parent_dock_id") or 0))
    parent_data = _payload(parent) if parent else {}
    display_price = data.get("sub_dock_price") if int(data.get("level") or 1) == 2 and data.get("sub_dock_price") else card_data.get("price")
    owner = users.get(owner_id)
    source_user = users.get(int(data.get("source_user_id") or 0))
    return {
        "id": row.id,
        "user_id": row.owner_id,
        "owner_username": users.get(row.owner_id).username if users.get(row.owner_id) else None,
        "card_id": data.get("card_id"),
        "card_name": card_data.get("name"),
        "dock_name": data.get("dock_name") or card_data.get("name") or "未命名对接",
        "markup_amount": str(data.get("markup_amount") or "0"),
        "card_price": str(card_data.get("price") or "0"),
        "fee_payer": card_data.get("fee_payer"),
        "min_price": str(card_data.get("min_price") or "0"),
        "is_multi_spec": _bool(card_data.get("is_multi_spec")),
        "spec_name": card_data.get("spec_name"),
        "spec_value": card_data.get("spec_value"),
        "remark": data.get("remark"),
        "delivery_count": int(data.get("delivery_count") or 0),
        "status": _bool(data.get("status"), row.status == "active"),
        "disable_reason": data.get("disable_reason"),
        "owner_disabled": _bool(data.get("owner_disabled")),
        "level": int(data.get("level") or 1),
        "parent_dock_id": data.get("parent_dock_id"),
        "source_user_id": data.get("source_user_id"),
        "allow_sub_dock": _bool(data.get("allow_sub_dock")),
        "sub_dock_price": data.get("sub_dock_price"),
        "sub_dock_visibility": data.get("sub_dock_visibility"),
        "display_price": str(display_price or "0"),
        "upstream_name": source_user.username if source_user else None,
        "owner_name": owner.username if owner else None,
        "parent_sub_dock_price": parent_data.get("sub_dock_price"),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


async def _visible_owner_ids(db: AsyncSession, current_id: int) -> set[int]:
    bindings = await _records(db, BINDING_FEATURE, current_id)
    return {int(_payload(item).get("target_user_id")) for item in bindings if str(_payload(item).get("target_user_id") or "").isdigit()}


@router.get("/supply")
async def get_supply(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=10000), search: str = Query(""), type: str = Query(""),
    user=Depends(get_current_user), db: AsyncSession = Depends(get_session),
):
    current_id = _uid(user)
    bound_owner_ids = await _visible_owner_ids(db, current_id)
    docks = {int(_payload(item).get("card_id")): item for item in await _records(db, DOCK_FEATURE, current_id) if int(_payload(item).get("level") or 1) == 1 and _bool(_payload(item).get("status"), True)}
    result: list[dict[str, Any]] = []
    for row in await _records(db, CARD_FEATURE):
        data = _payload(row)
        visibility = str(data.get("dock_visibility") or "public")
        if row.owner_id == current_id or not _card_is_enabled(row) or not _bool(data.get("is_dockable")):
            continue
        if visibility == "dealer_only" and row.owner_id not in bound_owner_ids:
            continue
        if type and str(data.get("type") or "text") != type:
            continue
        if search and search.lower() not in " ".join(str(data.get(key) or "") for key in ("name", "description", "text_content")).lower():
            continue
        result.append(_public_card(row, current_id, docks))
    return ok(_page(result, page, page_size), "货源查询成功")


@router.post("/dock-records")
async def create_dock(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    data = payload or {}
    try:
        card_id = int(data.get("card_id") or 0)
    except (TypeError, ValueError):
        card_id = 0
    card = await _card(db, card_id)
    current_id = _uid(user)
    if card is None:
        return {"success": False, "message": "卡券不存在"}
    card_data = _payload(card)
    if card.owner_id == current_id:
        return {"success": False, "message": "不能对接自己的卡券"}
    if not _card_is_enabled(card) or not _bool(card_data.get("is_dockable")):
        return {"success": False, "message": "该卡券未开放对接"}
    if str(card_data.get("dock_visibility") or "public") == "dealer_only" and card.owner_id not in await _visible_owner_ids(db, current_id):
        return {"success": False, "message": "请先绑定供应商对接码"}
    existing = [item for item in await _records(db, DOCK_FEATURE, current_id) if int(_payload(item).get("card_id") or 0) == card_id and int(_payload(item).get("level") or 1) == 1]
    if existing:
        return {"success": False, "message": "该卡券已经对接"}
    row = FeatureRecord(owner_id=current_id, feature=DOCK_FEATURE, external_id=uuid4().hex, status="active", payload={
        "card_id": card_id, "card_owner_id": card.owner_id, "dock_name": str(data.get("dock_name") or card_data.get("name") or "未命名对接").strip(),
        "markup_amount": str(data.get("markup_amount") or "0"), "remark": data.get("remark"), "status": True, "level": 1,
        "owner_disabled": False, "allow_sub_dock": False, "delivery_count": 0,
    })
    db.add(row); await db.commit(); await db.refresh(row)
    return ok({"id": row.id}, "对接成功")


async def _update_dock(row: FeatureRecord, changes: dict[str, Any], db: AsyncSession) -> None:
    data = _payload(row)
    allowed = {"dock_name", "markup_amount", "remark", "status", "disable_reason", "allow_sub_dock", "sub_dock_price", "sub_dock_visibility"}
    data.update({key: value for key, value in changes.items() if key in allowed})
    data["status"] = _bool(data.get("status"), True)
    row.status = "active" if data["status"] else "disabled"
    row.payload = data
    await db.commit(); await db.refresh(row)


@router.get("/dock-records")
async def list_docks(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=10000), search: str = Query(""), status: bool | None = Query(None), level: int | None = Query(None), allow_sub_dock: bool | None = Query(None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    current_id = _uid(user)
    rows = await _records(db, DOCK_FEATURE, None if _is_admin(user) else current_id)
    cards = {row.id: row for row in await _records(db, CARD_FEATURE)}
    users = await _users(db); all_docks = {row.id: row for row in await _records(db, DOCK_FEATURE)}
    result = []
    for row in rows:
        data = _payload(row)
        if status is not None and _bool(data.get("status"), row.status == "active") != status: continue
        if level is not None and int(data.get("level") or 1) != level: continue
        if allow_sub_dock is not None and _bool(data.get("allow_sub_dock")) != allow_sub_dock: continue
        if search and search.lower() not in str(data.get("dock_name") or "").lower(): continue
        result.append(await _dock_view(row, cards, users, all_docks))
    return ok(_page(result, page, page_size), "对接记录查询成功")


@router.put("/dock-records/{record_id}")
async def update_dock(record_id: int, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _dock(db, record_id)
    if row is None or (not _is_admin(user) and row.owner_id != _uid(user)):
        return {"success": False, "message": "对接记录不存在或无权限"}
    data = _payload(row)
    if _bool((payload or {}).get("status")) and _bool(data.get("owner_disabled")) and not _is_admin(user):
        return {"success": False, "message": "该对接记录已被上级禁用，无法自行启用"}
    await _update_dock(row, payload or {}, db)
    return {"success": True, "message": "更新成功"}


@router.delete("/dock-records/{record_id}")
async def delete_dock(record_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _dock(db, record_id)
    if row is None or (not _is_admin(user) and row.owner_id != _uid(user)):
        return {"success": False, "message": "对接记录不存在或无权限"}
    await db.execute(delete(FeatureRecord).where(FeatureRecord.feature == DOCK_FEATURE, FeatureRecord.id == record_id))
    await db.commit()
    return ok({"id": record_id, "deleted": True}, "删除成功")


@router.put("/dock-records/{record_id}/owner-update")
async def owner_update(record_id: int, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _dock(db, record_id); data = _payload(row) if row else {}
    card = await _card(db, int(data.get("card_id") or 0)) if row else None
    if row is None or card is None or card.owner_id != _uid(user): return {"success": False, "message": "对接记录不存在或无权限"}
    changes = {key: value for key, value in (payload or {}).items() if key in {"status", "disable_reason"}}
    if "status" in changes:
        changes["owner_disabled"] = not _bool(changes["status"])
        data["owner_disabled"] = not _bool(changes["status"])
        if not _bool(changes["status"]): changes["disable_reason"] = changes.get("disable_reason") or "货主已禁用"
        row.payload = data
        await _update_dock(row, changes, db)
        if not _bool(changes["status"]):
            for child in await _records(db, DOCK_FEATURE, None):
                child_data = _payload(child)
                if int(child_data.get("parent_dock_id") or 0) == record_id:
                    child_data.update({"status": False, "owner_disabled": True, "disable_reason": "上级分销商被禁用"}); child.status = "disabled"; child.payload = child_data
            await db.commit()
    return {"success": True, "message": "更新成功"}


@router.put("/dock-records/{record_id}/toggle-sub-dock")
async def toggle_sub_dock(record_id: int, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _dock(db, record_id)
    if row is None or int(_payload(row).get("level") or 1) != 1 or (not _is_admin(user) and row.owner_id != _uid(user)):
        return {"success": False, "message": "对接记录不存在或无权限"}
    data = payload or {}; allow = _bool(data.get("allow")); changes = {"allow_sub_dock": allow}
    if allow: changes.update({"sub_dock_price": data.get("sub_dock_price"), "sub_dock_visibility": data.get("sub_dock_visibility") or "public"})
    else: changes.update({"sub_dock_price": None, "sub_dock_visibility": None})
    await _update_dock(row, changes, db)
    return {"success": True, "message": f"{'开放' if allow else '关闭'}下级对接成功"}


@router.put("/dock-records/{record_id}/cascade-status")
async def cascade_status(record_id: int, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _dock(db, record_id); data = _payload(row) if row else {}
    card = await _card(db, int(data.get("card_id") or 0)) if row else None
    if row is None or card is None or card.owner_id != _uid(user): return {"success": False, "message": "对接记录不存在或无权限"}
    enabled = _bool((payload or {}).get("status")); data.update({"status": enabled, "owner_disabled": not enabled, "disable_reason": None if enabled else (payload or {}).get("disable_reason") or "货主已禁用"}); row.payload = data; row.status = "active" if enabled else "disabled"
    cascade = 0
    if not enabled:
        for child in await _records(db, DOCK_FEATURE, None):
            child_data = _payload(child)
            if int(child_data.get("parent_dock_id") or 0) == record_id:
                child_data.update({"status": False, "owner_disabled": True, "disable_reason": "上级分销商被禁用"}); child.status = "disabled"; child.payload = child_data; cascade += 1
    await db.commit()
    return {"success": True, "message": "状态更新成功", "data": {"cascade_count": cascade}}


@router.get("/source-bindings")
async def source_bindings(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    users = await _users(db); result = []
    for row in await _records(db, BINDING_FEATURE, _uid(user)):
        data = _payload(row); target_id = int(data.get("target_user_id") or 0)
        result.append({"id": row.id, "dock_code": data.get("dock_code"), "target_user_id": target_id, "target_username": users.get(target_id).username if users.get(target_id) else data.get("target_username"), "created_at": _iso(row.created_at)})
    return {"success": True, "data": result}


@router.post("/source-bindings")
async def bind_source(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    code = str((payload or {}).get("dock_code") or "").strip().upper(); current_id = _uid(user)
    if not code: return {"success": False, "message": "对接码不能为空"}
    candidates = await _records(db, f"{CREDENTIAL_PREFIX}dock_code")
    target = next((row for row in candidates if str(_payload(row).get("value") or "").upper() == code), None)
    if target is None: return {"success": False, "message": "对接码无效，未找到对应的供应商"}
    if target.owner_id == current_id: return {"success": False, "message": "不能绑定自己的对接码"}
    if any(int(_payload(row).get("target_user_id") or 0) == target.owner_id for row in await _records(db, BINDING_FEATURE, current_id)): return {"success": False, "message": "已绑定该供应商的对接码"}
    users = await _users(db); db.add(FeatureRecord(owner_id=current_id, feature=BINDING_FEATURE, external_id=uuid4().hex, status="active", payload={"dock_code": code, "target_user_id": target.owner_id, "target_username": users.get(target.owner_id).username if users.get(target.owner_id) else ""})); await db.commit()
    return {"success": True, "message": "货源绑定成功"}


@router.delete("/source-bindings/{binding_id}")
async def unbind_source(binding_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(FeatureRecord).where(FeatureRecord.id == binding_id, FeatureRecord.feature == BINDING_FEATURE, FeatureRecord.owner_id == _uid(user)))).scalar_one_or_none()
    if row is None: return {"success": False, "message": "绑定记录不存在"}
    target_id = int(_payload(row).get("target_user_id") or 0)
    for dock in await _records(db, DOCK_FEATURE, _uid(user)):
        if int(_payload(dock).get("card_owner_id") or 0) == target_id: await db.delete(dock)
    await db.delete(row); await db.commit()
    return {"success": True, "message": "已解绑，相关对接记录已清除"}


@router.get("/bound-to-me")
async def bound_to_me(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    users = await _users(db); result = []
    for row in await _records(db, BINDING_FEATURE):
        data = _payload(row)
        if int(data.get("target_user_id") or 0) != _uid(user): continue
        count = sum(1 for dock in await _records(db, DOCK_FEATURE, row.owner_id) if int(_payload(dock).get("card_owner_id") or 0) == _uid(user) and int(_payload(dock).get("level") or 1) == 1)
        result.append({"id": row.id, "user_id": row.owner_id, "username": users.get(row.owner_id).username if users.get(row.owner_id) else "", "dock_code": data.get("dock_code"), "dock_count": count, "created_at": _iso(row.created_at)})
    return {"success": True, "data": result}


@router.delete("/bound-to-me/{binding_id}")
async def remove_bound_user(binding_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(FeatureRecord).where(FeatureRecord.id == binding_id, FeatureRecord.feature == BINDING_FEATURE))).scalar_one_or_none()
    if row is None or int(_payload(row).get("target_user_id") or 0) != _uid(user): return {"success": False, "message": "绑定记录不存在"}
    for dock in await _records(db, DOCK_FEATURE, row.owner_id):
        if int(_payload(dock).get("card_owner_id") or 0) == _uid(user): await db.delete(dock)
    await db.delete(row); await db.commit(); return {"success": True, "message": "已删除，相关对接记录已清除"}


async def _dock_records_for_owner(db: AsyncSession, owner_id: int, *, level: int | None = None) -> list[FeatureRecord]:
    rows = await _records(db, DOCK_FEATURE, owner_id)
    return [row for row in rows if level is None or int(_payload(row).get("level") or 1) == level]


@router.get("/dealers")
async def dealers(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=10000), search: str = Query(""), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    current_id = _uid(user); users = await _users(db); cards = {row.id: row for row in await _records(db, CARD_FEATURE)}; all_docks = {row.id: row for row in await _records(db, DOCK_FEATURE)}
    grouped: dict[int, list[FeatureRecord]] = {}
    for row in await _records(db, DOCK_FEATURE):
        data = _payload(row); card = cards.get(int(data.get("card_id") or 0))
        if int(data.get("level") or 1) == 1 and card and card.owner_id == current_id: grouped.setdefault(row.owner_id, []).append(row)
    result = []
    for dealer_id, rows in grouped.items():
        name = users.get(dealer_id).username if users.get(dealer_id) else str(dealer_id)
        if search and search.lower() not in name.lower(): continue
        result.append({"user_id": dealer_id, "username": name, "email": users.get(dealer_id).email if users.get(dealer_id) else "", "dock_count": len(rows), "last_dock_time": _iso(rows[0].created_at), "level_1_count": len(rows), "level_2_count": 0})
    return ok(_page(result, page, page_size), "分销商查询成功")


@router.get("/dealers/{dealer_user_id}/details")
async def dealer_details(dealer_user_id: int, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=10000), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    cards = {row.id: row for row in await _records(db, CARD_FEATURE)}; rows = [row for row in await _dock_records_for_owner(db, dealer_user_id, level=1) if cards.get(int(_payload(row).get("card_id") or 0)) and cards[int(_payload(row).get("card_id") or 0)].owner_id == _uid(user)]; users = await _users(db); all_docks = {row.id: row for row in await _records(db, DOCK_FEATURE)}
    return ok(_page([await _dock_view(row, cards, users, all_docks) for row in rows], page, page_size), "分销商明细查询成功")


@router.get("/sub-supply")
async def sub_supply(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=10000), search: str = Query(""), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    current_id = _uid(user); cards = {row.id: row for row in await _records(db, CARD_FEATURE)}; users = await _users(db); all_docks = {row.id: row for row in await _records(db, DOCK_FEATURE)}; visible = await _visible_owner_ids(db, current_id); result = []
    for row in await _records(db, DOCK_FEATURE):
        data = _payload(row); card = cards.get(int(data.get("card_id") or 0)); visibility = str(data.get("sub_dock_visibility") or "public")
        if int(data.get("level") or 1) != 1 or row.owner_id == current_id or not _bool(data.get("status"), True) or not _bool(data.get("allow_sub_dock")) or (visibility == "dealer_only" and row.owner_id not in visible) or card is None: continue
        item = await _dock_view(row, cards, users, all_docks); item.update({"source_user_id": row.owner_id, "source_username": users.get(row.owner_id).username if users.get(row.owner_id) else "", "sub_dock_price": data.get("sub_dock_price"), "is_docked": any(int(_payload(child).get("parent_dock_id") or 0) == row.id and child.owner_id == current_id for child in all_docks.values())})
        if search and search.lower() not in f"{item.get('dock_name')} {item.get('card_name')}".lower(): continue
        result.append(item)
    return ok(_page(result, page, page_size), "二级货源查询成功")


@router.post("/sub-dock-records")
async def create_sub_dock(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    data = payload or {}; current_id = _uid(user)
    try: parent_id = int(data.get("parent_dock_id") or 0)
    except (TypeError, ValueError): parent_id = 0
    parent = await _dock(db, parent_id)
    if parent is None: return {"success": False, "message": "上级对接记录不存在"}
    parent_data = _payload(parent)
    if int(parent_data.get("level") or 1) != 1 or not _bool(parent_data.get("allow_sub_dock")) or not _bool(parent_data.get("status"), True): return {"success": False, "message": "该一级分销记录未开放下级对接"}
    if parent.owner_id == current_id: return {"success": False, "message": "不能对接自己的记录"}
    if any(int(_payload(row).get("parent_dock_id") or 0) == parent_id for row in await _dock_records_for_owner(db, current_id, level=2)): return {"success": False, "message": "您已对接该记录"}
    row = FeatureRecord(owner_id=current_id, feature=DOCK_FEATURE, external_id=uuid4().hex, status="active", payload={"card_id": parent_data.get("card_id"), "card_owner_id": parent_data.get("card_owner_id"), "dock_name": str(data.get("dock_name") or parent_data.get("dock_name") or "未命名对接"), "markup_amount": str(data.get("markup_amount") or "0"), "remark": data.get("remark"), "status": True, "level": 2, "parent_dock_id": parent_id, "source_user_id": parent.owner_id, "sub_dock_price": parent_data.get("sub_dock_price"), "owner_disabled": False, "delivery_count": 0}); db.add(row); await db.commit(); await db.refresh(row)
    return {"success": True, "message": "二级对接成功", "data": {"id": row.id}}


@router.get("/sub-dealers")
async def sub_dealers(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=10000), search: str = Query(""), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    users = await _users(db); grouped: dict[int, list[FeatureRecord]] = {}
    for row in await _dock_records_for_owner(db, None if _is_admin(user) else _uid(user), level=2): grouped.setdefault(row.owner_id, []).append(row)
    result = []
    for dealer_id, rows in grouped.items():
        name = users.get(dealer_id).username if users.get(dealer_id) else str(dealer_id)
        if search and search.lower() not in name.lower(): continue
        result.append({"user_id": dealer_id, "username": name, "email": users.get(dealer_id).email if users.get(dealer_id) else "", "dock_count": len(rows), "level_1_count": 0, "level_2_count": len(rows), "last_dock_time": _iso(rows[0].created_at)})
    return ok(_page(result, page, page_size), "下级分销商查询成功")


@router.get("/sub-dealers/{dealer_user_id}/details")
async def sub_dealer_details(dealer_user_id: int, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=10000), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = [row for row in await _dock_records_for_owner(db, dealer_user_id, level=2) if int(_payload(row).get("source_user_id") or 0) == _uid(user)]; cards = {row.id: row for row in await _records(db, CARD_FEATURE)}; users = await _users(db); all_docks = {row.id: row for row in await _records(db, DOCK_FEATURE)}
    return ok(_page([await _dock_view(row, cards, users, all_docks) for row in rows], page, page_size), "下级分销商明细查询成功")


@router.put("/sub-dealers/{record_id}/disable")
async def disable_sub_dealer(record_id: int, disable_reason: str = Query(""), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _dock(db, record_id); data = _payload(row) if row else {}
    if row is None or int(data.get("level") or 1) != 2 or int(data.get("source_user_id") or 0) != _uid(user): return {"success": False, "message": "对接记录不存在或无权限"}
    data.update({"status": False, "disable_reason": disable_reason or "上级分销商已禁用"}); row.payload = data; row.status = "disabled"; await db.commit(); return {"success": True, "message": "禁用成功"}


@router.put("/sub-dealers/{record_id}/enable")
async def enable_sub_dealer(record_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _dock(db, record_id); data = _payload(row) if row else {}
    if row is None or int(data.get("level") or 1) != 2 or int(data.get("source_user_id") or 0) != _uid(user) or _bool(data.get("owner_disabled")): return {"success": False, "message": "对接记录不存在或无权限"}
    data.update({"status": True, "disable_reason": None}); row.payload = data; row.status = "active"; await db.commit(); return {"success": True, "message": "启用成功"}


@router.get("/dock-records/{record_id}/pickup-url")
async def pickup_url(record_id: int, request: Request, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _dock(db, record_id)
    if row is None or (not _is_admin(user) and row.owner_id != _uid(user)): return {"success": False, "message": "对接记录不存在或无权限"}
    key = await _credential(db, row.owner_id, "secret_key", create=True)
    public_url = str(getattr(settings, "frontend_public_url", "") or "").strip().rstrip("/") or str(request.base_url).rstrip("/")
    return {"success": True, "message": "获取成功", "data": {"pickup_url": f"{public_url}/api/v1/distribution/pickup?key={key}&dock_id={record_id}"}}


def _api_value(body: Any, field: str) -> Any:
    if field:
        current = body
        for part in field.split("."):
            if not isinstance(current, dict): return None
            current = current.get(part)
        return current
    return body


async def _pickup_content(db: AsyncSession, card: FeatureRecord, order_no: str) -> tuple[str, list[str]]:
    data = _payload(card); card_type = str(data.get("type") or "text"); reserved: list[str] = []
    if card_type == "text": content = str(data.get("text_content") or "").strip()
    elif card_type == "image":
        values = data.get("image_urls")
        if isinstance(values, str):
            try: values = json.loads(values)
            except (TypeError, ValueError, json.JSONDecodeError): values = [values]
        if not isinstance(values, list): values = [data.get("image_url")] if data.get("image_url") else []
        content = "\n".join(str(value) for value in values if str(value or "").strip())
    elif card_type == "data":
        locked = (await db.execute(select(FeatureRecord).where(FeatureRecord.id == card.id).with_for_update())).scalar_one_or_none()
        locked_data = _payload(locked) if locked else {}; lines = [line.strip() for line in str(locked_data.get("data_content") or "").splitlines() if line.strip()]
        if not lines: return "", []
        reserved = [lines[0]]; locked_data["data_content"] = "\n".join(lines[1:]); locked.payload = locked_data; card.payload = locked_data; content = reserved[0]
    elif card_type == "api":
        config = data.get("api_config") or {}; config = json.loads(config) if isinstance(config, str) else config
        url = str(config.get("url") or "").strip() if isinstance(config, dict) else ""
        if not url: return "", []
        async with httpx.AsyncClient(timeout=min(120, max(1, int(config.get("timeout") or 60))), follow_redirects=True) as client:
            response = await (client.post(url, json=config.get("params") or {}) if str(config.get("method") or "GET").upper() == "POST" else client.get(url, params=config.get("params") or {})); response.raise_for_status(); body = response.json()
        value = _api_value(body, str(config.get("response_field") or "")); content = json.dumps(value if value is not None else body, ensure_ascii=False) if isinstance(value if value is not None else body, (dict, list)) else str(value if value is not None else body)
    else: content = ""
    return content, reserved


@router.get("/pickup", response_class=PlainTextResponse)
async def pickup(key: str = Query(""), dock_id: int = Query(0), db: AsyncSession = Depends(get_session)):
    key = key.strip(); credential_rows = await _records(db, f"{CREDENTIAL_PREFIX}secret_key"); credential = next((row for row in credential_rows if str(_payload(row).get("value") or "") == key), None)
    if credential is None: return PlainTextResponse("提货失败：秘钥无效", status_code=200)
    row = await _dock(db, dock_id); data = _payload(row) if row else {}
    if row is None or row.owner_id != credential.owner_id or not _bool(data.get("status"), True): return PlainTextResponse("提货失败：对接记录不存在或已停用", status_code=200)
    card = await _card(db, int(data.get("card_id") or 0))
    if card is None or not _card_is_enabled(card): return PlainTextResponse("提货失败：卡券不存在或已停用", status_code=200)
    now = datetime.now(timezone.utc).replace(tzinfo=None); recent = [item for item in await _records(db, AGENT_FEATURE, row.owner_id) if int(_payload(item).get("dock_record_id") or 0) == dock_id and item.created_at and item.created_at > now - timedelta(seconds=5)]
    if recent: return PlainTextResponse("提货失败：请求过于频繁，请 5 秒后再试", status_code=200)
    order_no = f"PICKUP-{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4).upper()}"
    try:
        content, reserved = await _pickup_content(db, card, order_no)
    except Exception as exc:  # noqa: BLE001
        # API 卡券的上游异常、坏 JSON 或图片字段异常都必须以业务失败返回，
        # 不能让免认证提货链接变成 500 页面。
        await db.rollback()
        return PlainTextResponse(f"提货失败：卡券内容获取失败：{str(exc)[:300]}", status_code=200)
    if not content: return PlainTextResponse("提货失败：卡券内容获取失败或库存不足", status_code=200)
    card_data = _payload(card); card_price = _decimal(card_data.get("price")); dock_price = _decimal(data.get("sub_dock_price") if int(data.get("level") or 1) == 2 and data.get("sub_dock_price") else card_price); fee_type = (await db.execute(select(SystemSetting.setting_value).where(SystemSetting.setting_key == "distribution.fee_type"))).scalar_one_or_none() or "fixed"; fee_rate = _decimal((await db.execute(select(SystemSetting.setting_value).where(SystemSetting.setting_key == "distribution.fee_rate"))).scalar_one_or_none()); fee = (dock_price * fee_rate / Decimal("100")).quantize(Decimal("0.01")) if fee_type == "percent" else fee_rate
    users = await _users(db); dealer = users.get(row.owner_id); owner = users.get(card.owner_id); source_id = int(data.get("source_user_id") or 0) if int(data.get("level") or 1) == 2 else card.owner_id; source = users.get(source_id)
    if dealer is None or owner is None: return PlainTextResponse("提货失败：参与方不存在", status_code=200)
    dealer_cost = dock_price + (fee if str(card_data.get("fee_payer") or "distributor") == "dealer" else Decimal("0")); owner_gain = card_price - (fee if str(card_data.get("fee_payer") or "distributor") != "dealer" else Decimal("0")); middle_gain = dock_price - card_price if int(data.get("level") or 1) == 2 else Decimal("0")
    if _decimal(dealer.balance) < dealer_cost:
        return PlainTextResponse(f"提货失败：您的余额不足（当前余额 {_decimal(dealer.balance)}，本单需 {_decimal(dealer_cost)}）", status_code=200)
    dealer.balance = _decimal(dealer.balance) - dealer_cost; owner.balance = _decimal(owner.balance) + owner_gain
    if source is not None and int(data.get("level") or 1) == 2: source.balance = _decimal(source.balance) + middle_gain
    agent_payload = {"order_no": order_no, "item_id": card_data.get("item_id") or "", "card_id": card.id, "dock_record_id": row.id, "dock_level": int(data.get("level") or 1), "sale_price": str(dock_price), "dock_price": str(dock_price), "card_price": str(card_price), "level2_cost": str(dock_price) if int(data.get("level") or 1) == 2 else "0", "profit": "0.00", "fee_amount": str(fee), "fee_payer": card_data.get("fee_payer"), "owner_user_id": card.owner_id, "upstream_user_id": source_id, "delivery_content": content[:2000], "buyer_id": "pickup", "status": "settled", "source": "pickup"}
    agent = FeatureRecord(owner_id=row.owner_id, feature=AGENT_FEATURE, external_id=order_no, status="settled", payload=agent_payload); db.add(agent); ddata = _payload(row); ddata["delivery_count"] = int(ddata.get("delivery_count") or 0) + 1; row.payload = ddata
    for user_row, amount, description in ((dealer, -dealer_cost, "提货支出"), (owner, owner_gain, "卡券收入"), (source, middle_gain, "二级分销差价")):
        if user_row is not None and amount != 0: db.add(FeatureRecord(owner_id=user_row.id, feature=FLOW_FEATURE, external_id=uuid4().hex, status="posted", payload={"user_id": user_row.id, "type": "income" if amount > 0 else "expense", "amount": str(abs(amount)), "balance_after": str(_decimal(user_row.balance)), "order_id": order_no, "dock_record_id": row.id, "description": description}))
    await db.commit(); return PlainTextResponse(content.replace("######", "\n"), status_code=200)


@router.get("/order-delivery")
async def order_delivery(order_no: str = Query(""), key: str = Query(""), db: AsyncSession = Depends(get_session)):
    credential_rows = await _records(db, f"{CREDENTIAL_PREFIX}secret_key"); credential = next((row for row in credential_rows if str(_payload(row).get("value") or "") == key.strip()), None)
    if credential is None: return {"success": False, "message": "秘钥无效或不存在"}
    row = next((item for item in await _records(db, AGENT_FEATURE) if str(_payload(item).get("order_no") or "") == order_no and int(_payload(item).get("owner_user_id") or 0) == credential.owner_id), None)
    if row is None: return {"success": False, "message": "订单不存在或不属于当前秘钥"}
    data = _payload(row); return {"success": True, "message": "获取成功", "data": {"order_no": order_no, "delivery_content": data.get("delivery_content") or "", "delivery_method": "pickup", "status": data.get("status"), "delivered": bool(data.get("delivery_content"))}}


@router.post("/order-delivery")
async def order_delivery_post(payload: dict[str, Any] | None = Body(default=None), db: AsyncSession = Depends(get_session)):
    data = payload or {}; return await order_delivery(str(data.get("order_no") or ""), str(data.get("key") or ""), db)


def _agent_view(row: FeatureRecord, users: dict[int, User]) -> dict[str, Any]:
    data = _payload(row); dealer_id = row.owner_id; owner_id = int(data.get("owner_user_id") or 0); upstream_id = int(data.get("upstream_user_id") or 0)
    return {**data, "id": row.id, "user_id": dealer_id, "user_name": users.get(dealer_id).username if users.get(dealer_id) else None, "owner_name": users.get(owner_id).username if users.get(owner_id) else None, "upstream_name": users.get(upstream_id).username if users.get(upstream_id) else None, "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at)}


@router.get("/agent-orders/my")
async def agent_orders_my(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=10000), status: str = Query(""), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    users = await _users(db); rows = [row for row in await _records(db, AGENT_FEATURE, _uid(user)) if not status or str(_payload(row).get("status") or "") == status]; return {"success": True, "data": _page([_agent_view(row, users) for row in rows], page, page_size)}


@router.get("/agent-orders/upstream")
async def agent_orders_upstream(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=10000), status: str = Query(""), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    users = await _users(db); rows = [row for row in await _records(db, AGENT_FEATURE) if int(_payload(row).get("owner_user_id") or 0) == _uid(user) and (not status or str(_payload(row).get("status") or "") == status)]; return {"success": True, "data": _page([_agent_view(row, users) for row in rows], page, page_size)}


@router.get("/agent-orders/detail/{order_id}")
async def agent_order_detail(order_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(FeatureRecord).where(FeatureRecord.id == order_id, FeatureRecord.feature == AGENT_FEATURE))).scalar_one_or_none(); data = _payload(row) if row else {}
    if row is None or (_uid(user) not in {row.owner_id, int(data.get("owner_user_id") or 0), int(data.get("upstream_user_id") or 0)} and not _is_admin(user)): return {"success": False, "message": "订单不存在或无权查看"}
    return {"success": True, "data": _agent_view(row, await _users(db))}


@router.get("/fund-flows")
async def fund_flows(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=10000), type: str = Query(""), username: str = Query(""), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    users = await _users(db); rows = await _records(db, FLOW_FEATURE, None if _is_admin(user) else _uid(user)); result = []
    for row in rows:
        data = _payload(row); subject = users.get(row.owner_id); name = subject.username if subject else ""
        if type and data.get("type") != type: continue
        if username and username.lower() not in name.lower(): continue
        result.append({**data, "id": row.id, "user_id": row.owner_id, "username": name, "created_at": _iso(row.created_at)})
    return ok(_page(result, page, page_size), "资金流水查询成功")
