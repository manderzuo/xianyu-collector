# -*- coding: utf-8 -*-
"""消息过滤规则的结构化 CRUD。"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models import FeatureRecord

router = APIRouter(prefix="/api/v1/message-filters", tags=["消息过滤"])
FEATURE = "message-filters"


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _rows(user: dict[str, Any], db: AsyncSession):
    return db.execute(select(FeatureRecord).where(FeatureRecord.feature == FEATURE, FeatureRecord.owner_id == _uid(user)).order_by(FeatureRecord.id.desc()))


def _serialize(item: FeatureRecord) -> dict[str, Any]:
    data = dict(item.payload or {})
    data.update({"id": item.id, "enabled": item.status == "active", "created_at": item.created_at, "updated_at": item.updated_at})
    return data


async def _get(rule_id: int, user: dict[str, Any], db: AsyncSession) -> FeatureRecord:
    item = (await db.execute(select(FeatureRecord).where(FeatureRecord.id == rule_id, FeatureRecord.feature == FEATURE, FeatureRecord.owner_id == _uid(user)))).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="过滤规则不存在")
    return item


@router.get("")
@router.get("/")
async def list_filters(account_id: str | None = Query(None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = (await _rows(user, db)).scalars().all()
    items = [_serialize(row) for row in rows if not account_id or str((row.payload or {}).get("account_id")) == str(account_id)]
    return ok(items, "过滤规则查询成功")


@router.post("")
@router.post("/")
async def create_filter(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    data = dict(payload or {})
    account_id = str(data.get("account_id") or "").strip()
    keyword = str(data.get("keyword") or "").strip()
    filter_types = data.get("filter_types") or [data.get("filter_type")]
    filter_types = [str(value) for value in filter_types if value]
    if not account_id or not keyword or not filter_types:
        raise HTTPException(status_code=422, detail="账号、关键词和过滤类型不能为空")
    created = []
    for filter_type in filter_types:
        item = FeatureRecord(owner_id=_uid(user), feature=FEATURE, external_id=uuid4().hex, status="active", payload={"account_id": account_id, "keyword": keyword, "filter_type": filter_type})
        db.add(item)
        created.append(item)
    await db.commit()
    for item in created:
        await db.refresh(item)
    return ok({"created_ids": [item.id for item in created], "items": [_serialize(item) for item in created]}, "过滤规则创建成功")


@router.post("/batch-create")
async def create_filters_batch(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    data = payload or {}
    account_ids = [str(value) for value in (data.get("account_ids") or []) if str(value).strip()]
    filter_types = [str(value) for value in (data.get("filter_types") or []) if str(value).strip()]
    keyword = str(data.get("keyword") or "").strip()
    if not account_ids or not filter_types or not keyword:
        raise HTTPException(status_code=422, detail="账号、关键词和过滤类型不能为空")
    created = []
    for account_id in dict.fromkeys(account_ids):
        for filter_type in dict.fromkeys(filter_types):
            item = FeatureRecord(owner_id=_uid(user), feature=FEATURE, external_id=uuid4().hex, status="active", payload={"account_id": account_id, "keyword": keyword, "filter_type": filter_type})
            db.add(item)
            created.append(item)
    await db.commit()
    return ok({"created_ids": [item.id for item in created], "created_count": len(created), "failed_count": 0}, "过滤规则批量创建成功")


@router.put("/{rule_id}/toggle")
async def toggle_filter(rule_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = await _get(rule_id, user, db)
    item.status = "inactive" if item.status == "active" else "active"
    await db.commit()
    return ok({"enabled": item.status == "active", "id": item.id}, "过滤规则状态已更新")


@router.put("/{rule_id}")
async def update_filter(rule_id: int, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = await _get(rule_id, user, db)
    data = dict(item.payload or {})
    values = payload or {}
    for key in ("account_id", "keyword", "filter_type"):
        if key in values:
            data[key] = values[key]
    if "enabled" in values:
        item.status = "active" if values["enabled"] else "inactive"
    item.payload = data
    await db.commit()
    await db.refresh(item)
    return ok(_serialize(item), "过滤规则已更新")


@router.delete("/{rule_id}")
async def delete_filter(rule_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = await _get(rule_id, user, db)
    await db.delete(item)
    await db.commit()
    return ok({"id": rule_id, "deleted": True}, "过滤规则已删除")


@router.post("/batch-delete")
async def delete_filters_batch(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    ids = [int(value) for value in ((payload or {}).get("ids") or []) if str(value).isdigit()]
    if ids:
        await db.execute(delete(FeatureRecord).where(FeatureRecord.feature == FEATURE, FeatureRecord.owner_id == _uid(user), FeatureRecord.id.in_(ids)))
        await db.commit()
    return ok({"deleted_count": len(ids)}, "过滤规则已批量删除")
