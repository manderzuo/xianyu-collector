"""个人黑名单的真实持久化接口。"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Body, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models import FeatureRecord

router = APIRouter(prefix="/api/v1/blacklist", tags=["黑名单"])
PERSONAL = "personal-blacklist"
PLATFORM = "platform-blacklist"


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _data(row: FeatureRecord) -> dict[str, Any]:
    value = dict(row.payload or {})
    return {
        "id": row.id, "owner_id": row.owner_id,
        "account_id": value.get("account_id"), "buyer_id": str(value.get("buyer_id") or ""),
        "buyer_nick": value.get("buyer_nick"), "item_id": value.get("item_id"),
        "reason": value.get("reason"), "is_enabled": bool(value.get("is_enabled", True)),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _personal(record_id: int, user: dict[str, Any], db: AsyncSession) -> FeatureRecord:
    statement = select(FeatureRecord).where(FeatureRecord.id == record_id, FeatureRecord.feature == PERSONAL)
    if not _admin(user):
        statement = statement.where(FeatureRecord.owner_id == _uid(user))
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "黑名单记录不存在或无权访问")
    return row


@router.get("/personal")
@router.get("/personal/")
async def list_personal(
    buyer_id: str | None = None, buyer_nick: str | None = None,
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200),
    user=Depends(get_current_user), db: AsyncSession = Depends(get_session),
):
    statement = select(FeatureRecord).where(FeatureRecord.feature == PERSONAL).order_by(desc(FeatureRecord.id))
    if not _admin(user): statement = statement.where(FeatureRecord.owner_id == _uid(user))
    values = [_data(row) for row in (await db.execute(statement)).scalars().all()]
    if buyer_id: values = [item for item in values if buyer_id.lower() in item["buyer_id"].lower()]
    if buyer_nick: values = [item for item in values if buyer_nick.lower() in str(item.get("buyer_nick") or "").lower()]
    total = len(values); start = (page - 1) * page_size
    return ok({"items": values[start:start + page_size], "total": total, "page": page, "page_size": page_size})


@router.post("/personal")
@router.post("/personal/")
async def create_personal(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    raw_ids = str(payload.get("buyer_ids") or payload.get("buyer_id") or "")
    buyer_ids = [item.strip() for item in raw_ids.replace("\n", ",").split(",") if item.strip()]
    if not buyer_ids: raise HTTPException(422, "买家ID不能为空")
    created = []
    for buyer_id in dict.fromkeys(buyer_ids):
        row = FeatureRecord(owner_id=_uid(user), feature=PERSONAL, external_id=uuid4().hex, status="active", payload={
            "account_id": payload.get("account_id"), "buyer_id": buyer_id,
            "buyer_nick": payload.get("buyer_nick"), "item_id": payload.get("item_id"),
            "reason": payload.get("reason"), "is_enabled": bool(payload.get("is_enabled", True)),
        }, note="个人黑名单")
        db.add(row); created.append(row)
    await db.commit()
    for row in created: await db.refresh(row)
    return ok({"items": [_data(row) for row in created], "created": len(created)}, "黑名单已添加")


@router.get("/personal/export")
async def export_personal(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    statement = select(FeatureRecord).where(FeatureRecord.feature == PERSONAL)
    if not _admin(user): statement = statement.where(FeatureRecord.owner_id == _uid(user))
    output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=["buyer_id", "buyer_nick", "account_id", "item_id", "reason", "is_enabled"]); writer.writeheader()
    for row in (await db.execute(statement.order_by(FeatureRecord.id.asc()))).scalars().all():
        value = _data(row); writer.writerow({key: value.get(key, "") for key in ["buyer_id", "buyer_nick", "account_id", "item_id", "reason", "is_enabled"]})
    return StreamingResponse(iter([output.getvalue().encode("utf-8-sig")]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=personal-blacklist.csv"})


@router.post("/personal/import")
async def import_personal(file: UploadFile = File(...), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    raw = await file.read()
    try: rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    except UnicodeDecodeError as exc: raise HTTPException(422, "导入文件必须是UTF-8 CSV") from exc
    created = 0
    for row in rows:
        buyer_id = str(row.get("buyer_id") or row.get("买家ID") or "").strip()
        if not buyer_id: continue
        db.add(FeatureRecord(owner_id=_uid(user), feature=PERSONAL, external_id=uuid4().hex, status="active", payload={"buyer_id": buyer_id, "buyer_nick": row.get("buyer_nick") or row.get("买家昵称"), "account_id": row.get("account_id"), "item_id": row.get("item_id"), "reason": row.get("reason"), "is_enabled": str(row.get("is_enabled", "true")).lower() not in {"0", "false", "否"}}, note="个人黑名单导入")); created += 1
    await db.commit()
    return ok({"created": created}, "黑名单导入完成")


@router.delete("/personal/{record_id}")
async def delete_personal(record_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _personal(record_id, user, db); await db.delete(row); await db.commit()
    return ok({"id": record_id, "deleted": True}, "删除成功")


@router.post("/personal/batch-delete")
async def batch_delete(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    ids = [int(value) for value in payload.get("ids") or [] if str(value).isdigit()]
    statement = delete(FeatureRecord).where(FeatureRecord.feature == PERSONAL, FeatureRecord.id.in_(ids))
    if not _admin(user): statement = statement.where(FeatureRecord.owner_id == _uid(user))
    result = await db.execute(statement); await db.commit()
    return ok({"deleted": int(result.rowcount or 0), "ids": ids}, "批量删除成功")


@router.patch("/personal/{record_id}/toggle")
async def toggle_personal(record_id: int, payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _personal(record_id, user, db); value = dict(row.payload or {}); value["is_enabled"] = bool(payload.get("is_enabled", not bool(value.get("is_enabled", True))))
    row.payload = value; await db.commit(); await db.refresh(row)
    return ok(_data(row), "黑名单状态已更新")


@router.get("/platform")
async def platform_blacklist(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    statement = select(FeatureRecord).where(FeatureRecord.feature == PLATFORM).order_by(desc(FeatureRecord.id))
    if not _admin(user): statement = statement.where(FeatureRecord.owner_id == _uid(user))
    rows = [_data(row) for row in (await db.execute(statement)).scalars().all()]
    start = (page - 1) * page_size
    return ok({"items": rows[start:start + page_size], "total": len(rows), "page": page, "page_size": page_size})


__all__ = ["router"]
