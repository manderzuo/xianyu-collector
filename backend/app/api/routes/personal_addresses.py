# -*- coding: utf-8 -*-
"""商品发布个人地址库的真实持久化接口。"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models import FeatureRecord

router = APIRouter(prefix="/api/v1/product-publish/personal-addresses", tags=["个人地址库"])
FEATURE = "personal-addresses"


def _uid(user: dict) -> int:
    try: return int(user.get("sub", 1))
    except (TypeError, ValueError): return 1


def _address(row: FeatureRecord) -> dict[str, Any]:
    value = dict(row.payload or {})
    return {"id": row.id, "address": str(value.get("address") or ""), "use_count": int(value.get("use_count") or 0), "last_used_at": value.get("last_used_at"), "created_at": row.created_at, "updated_at": row.updated_at}


async def _rows(user: dict, db: AsyncSession) -> list[FeatureRecord]:
    return list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == FEATURE, FeatureRecord.owner_id == _uid(user), FeatureRecord.status != "deleted").order_by(desc(FeatureRecord.id)))).scalars().all())


@router.get("")
@router.get("/")
async def list_addresses(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), keyword: str | None = None, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = await _rows(user, db)
    values = [_address(row) for row in rows]
    if keyword:
        values = [item for item in values if keyword.casefold() in item["address"].casefold()]
    total = len(values); start = (page - 1) * page_size
    return ok({"list": values[start:start + page_size], "items": values[start:start + page_size], "total": total, "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size if total else 0}, "地址查询成功")


@router.post("")
@router.post("/")
async def create_address(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    value = str(payload.get("address") or "").strip()
    if not value: raise HTTPException(422, "地址不能为空")
    rows = await _rows(user, db)
    existing = next((row for row in rows if str((row.payload or {}).get("address") or "").strip() == value), None)
    if existing:
        return ok({"address": _address(existing)}, "地址已存在")
    row = FeatureRecord(owner_id=_uid(user), feature=FEATURE, external_id=uuid4().hex, status="active", payload={"address": value, "use_count": 0, "last_used_at": None}, note="个人发布地址")
    db.add(row); await db.commit(); await db.refresh(row)
    return ok({"address": _address(row)}, "地址已创建")


@router.get("/export")
async def export_addresses(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    output = io.StringIO(); writer = csv.writer(output); writer.writerow(["address", "use_count", "last_used_at"])
    for row in await _rows(user, db):
        value = _address(row); writer.writerow([value["address"], value["use_count"], value["last_used_at"] or ""])
    data = output.getvalue().encode("utf-8-sig")
    return StreamingResponse(io.BytesIO(data), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=personal-addresses.csv"})


@router.post("/import")
async def import_addresses(file: UploadFile = File(...), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    raw = await file.read(2 * 1024 * 1024 + 1)
    if not raw or len(raw) > 2 * 1024 * 1024: raise HTTPException(422, "导入文件为空或超过2MB")
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text)); rows = await _rows(user, db); by_address = {str((row.payload or {}).get("address") or "").strip(): row for row in rows if str((row.payload or {}).get("address") or "").strip()}
    created = updated = 0
    for line in reader:
        if not line: continue
        value = str(line[0] or "").strip()
        if not value or value.casefold() == "address": continue
        row = by_address.get(value)
        if row is None:
            row = FeatureRecord(owner_id=_uid(user), feature=FEATURE, external_id=uuid4().hex, status="active", payload={"address": value, "use_count": 0, "last_used_at": None}, note="地址导入"); db.add(row); by_address[value] = row; created += 1
        else:
            row.status = "active"; updated += 1
    await db.commit()
    return ok({"created": created, "updated": updated}, "地址导入完成")


async def _owned(address_id: int, user: dict, db: AsyncSession) -> FeatureRecord:
    row = (await db.execute(select(FeatureRecord).where(FeatureRecord.id == address_id, FeatureRecord.feature == FEATURE, FeatureRecord.owner_id == _uid(user), FeatureRecord.status != "deleted"))).scalar_one_or_none()
    if row is None: raise HTTPException(404, "地址不存在")
    return row


@router.put("/{address_id}")
async def update_address(address_id: int, payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _owned(address_id, user, db); value = str(payload.get("address") or "").strip()
    if not value: raise HTTPException(422, "地址不能为空")
    row.payload = {**(row.payload or {}), "address": value}; await db.commit(); await db.refresh(row)
    return ok({"address": _address(row)}, "地址已更新")


@router.post("/batch-delete")
async def batch_delete(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    ids = [int(item) for item in payload.get("ids") or [] if str(item).isdigit()]
    rows = list((await db.execute(select(FeatureRecord).where(FeatureRecord.id.in_(ids), FeatureRecord.feature == FEATURE, FeatureRecord.owner_id == _uid(user), FeatureRecord.status != "deleted"))).scalars().all()) if ids else []
    for row in rows: row.status = "deleted"
    await db.commit()
    return ok({"success_count": len(rows), "total_count": len(ids)}, "地址已批量删除")

