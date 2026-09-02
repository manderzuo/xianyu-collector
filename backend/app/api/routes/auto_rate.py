# -*- coding: utf-8 -*-
"""自动评价和批量补评价接口。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import error, ok
from common.db.session import get_session
from common.models import Account, FeatureRecord
from common.services.auto_rate import run_rate_batch

router = APIRouter(tags=["自动评价"])


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _visible(statement, user: dict[str, Any]):
    if not _admin(user):
        statement = statement.where(FeatureRecord.owner_id == _uid(user))
    return statement


def _batch_payload(row: FeatureRecord) -> dict[str, Any]:
    return {**(row.payload or {}), "batch_id": row.external_id, "status": row.status, "created_at": row.created_at, "updated_at": row.updated_at}


@router.post("/api/v1/auto-rate/batch-rate")
async def batch_rate(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    raw_ids = payload.get("account_ids") or []
    try:
        ids = list(dict.fromkeys(int(value) for value in raw_ids))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="账号ID格式无效") from exc
    if not ids:
        return error("请至少选择一个账号", code="account_selection_required")
    statement = select(Account).where(Account.id.in_(ids), Account.cookie.isnot(None), Account.cookie != "")
    if not _admin(user):
        statement = statement.where(Account.user_id == _uid(user))
    accounts = list((await db.execute(statement.order_by(Account.id.asc()))).scalars().all())
    if len(accounts) != len(ids):
        return error("部分账号不存在、未登录或无权使用，请重新选择账号", code="account_invalid")
    result = await run_rate_batch(db, accounts, scheduled_only=False)
    return ok(result, "批量补评价完成")


@router.get("/api/v1/admin/rate-batches")
async def list_rate_batches(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    start_date: str | None = Query(None), end_date: str | None = Query(None),
    user=Depends(get_current_user), db: AsyncSession = Depends(get_session),
):
    statement = _visible(select(FeatureRecord).where(FeatureRecord.feature == "rate-batches"), user)
    try:
        if start_date:
            statement = statement.where(FeatureRecord.created_at >= datetime.fromisoformat(start_date))
        if end_date:
            statement = statement.where(FeatureRecord.created_at < datetime.fromisoformat(end_date) + timedelta(days=1))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="日期格式无效") from exc
    rows = list((await db.execute(statement.order_by(FeatureRecord.id.desc()))).scalars().all())
    total = len(rows)
    start = (page - 1) * page_size
    values = [_batch_payload(row) for row in rows[start:start + page_size]]
    return ok({"items": values, "total": total, "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size if total else 0}, "查询成功")


@router.get("/api/v1/admin/rate-batches/{batch_id}")
async def get_rate_batch(batch_id: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    statement = _visible(select(FeatureRecord).where(FeatureRecord.feature == "rate-batches", FeatureRecord.external_id == batch_id), user)
    batch = (await db.execute(statement)).scalar_one_or_none()
    if batch is None:
        raise HTTPException(status_code=404, detail="评价批次不存在")
    logs_statement = _visible(select(FeatureRecord).where(FeatureRecord.feature == "rate-logs", FeatureRecord.payload["batch_id"].as_string() == batch_id), user)
    logs = list((await db.execute(logs_statement.order_by(FeatureRecord.id.asc()))).scalars().all())
    values = [{**(row.payload or {}), "id": row.id, "created_at": row.created_at, "status": row.status} for row in logs]
    return ok({**_batch_payload(batch), "logs": values}, "查询成功")


@router.delete("/api/v1/admin/rate-logs/clear")
async def clear_rate_logs(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    cutoff = datetime.utcnow() - timedelta(days=10)
    statement = delete(FeatureRecord).where(FeatureRecord.feature.in_({"rate-logs", "rate-batches"}), FeatureRecord.created_at < cutoff)
    if not _admin(user):
        statement = statement.where(FeatureRecord.owner_id == _uid(user))
    result = await db.execute(statement)
    await db.commit()
    return ok({"deleted": int(result.rowcount or 0)}, "10天前的评价日志已清理")


__all__ = ["router"]
