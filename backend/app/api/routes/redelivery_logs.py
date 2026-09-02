"""定时补发货批次及明细接口。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models import FeatureRecord

router = APIRouter(prefix="/api/v1/admin", tags=["定时补发货"])


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _guard(user: dict[str, Any]) -> None:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可以查看补发货日志")


def _bounds(start_date: str | None, end_date: str | None) -> tuple[datetime | None, datetime | None]:
    try:
        start = datetime.combine(date.fromisoformat(start_date), time.min) if start_date else None
        end = datetime.combine(date.fromisoformat(end_date) + timedelta(days=1), time.min) if end_date else None
        return start, end
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="日期格式必须为YYYY-MM-DD") from exc


def _batch_view(row: FeatureRecord) -> dict[str, Any]:
    payload = dict(row.payload or {})
    return {
        "batch_id": str(payload.get("batch_id") or row.external_id or row.id),
        "executed_at": payload.get("executed_at") or (row.created_at.isoformat() if row.created_at else None),
        "status": payload.get("status") or row.status,
        "account_count": int(payload.get("account_count") or 0),
        "total_orders": int(payload.get("total_orders") or 0),
        "success_count": int(payload.get("success_count") or 0),
        "failed_count": int(payload.get("failed_count") or 0),
        "skipped_count": int(payload.get("skipped_count") or 0),
        "failed_account_count": int(payload.get("failed_account_count") or 0),
    }


def _log_view(row: FeatureRecord) -> dict[str, Any]:
    payload = dict(row.payload or {})
    return {
        "id": row.id,
        "batch_id": str(payload.get("batch_id") or ""),
        "account_id": str(payload.get("account_id") or ""),
        "account_name": payload.get("account_name"),
        "order_no": str(payload.get("order_no") or ""),
        "status": payload.get("status") or row.status,
        "error_message": payload.get("error_message"),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/redelivery-batches")
async def list_redelivery_batches(start_date: str | None = None, end_date: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _guard(user)
    start, end = _bounds(start_date, end_date)
    statement = select(FeatureRecord).where(FeatureRecord.feature == "redelivery-batches").order_by(FeatureRecord.created_at.desc())
    if start:
        statement = statement.where(FeatureRecord.created_at >= start)
    if end:
        statement = statement.where(FeatureRecord.created_at < end)
    rows = list((await db.execute(statement)).scalars().all())
    items = [_batch_view(row) for row in rows]
    total = len(items)
    sliced = items[(page - 1) * page_size : page * page_size]
    return ok({"items": sliced, "total": total, "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size})


@router.get("/redelivery-batches/{batch_id}")
async def redelivery_batch_detail(batch_id: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _guard(user)
    batch_rows = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == "redelivery-batches").order_by(FeatureRecord.id.desc()))).scalars().all())
    batch = next((row for row in batch_rows if str((row.payload or {}).get("batch_id") or row.external_id) == batch_id), None)
    if batch is None:
        raise HTTPException(status_code=404, detail="补发货批次不存在")
    log_rows = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == "redelivery-logs").order_by(FeatureRecord.id.asc()))).scalars().all())
    logs = [_log_view(row) for row in log_rows if str((row.payload or {}).get("batch_id") or "") == batch_id]
    data = _batch_view(batch)
    data["logs"] = logs
    data["account_results"] = (batch.payload or {}).get("account_results") or []
    return ok(data)


@router.delete("/redelivery-logs/clear")
async def clear_redelivery_logs(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _guard(user)
    cutoff = datetime.now() - timedelta(days=10)
    result = await db.execute(delete(FeatureRecord).where(FeatureRecord.feature.in_(["redelivery-batches", "redelivery-logs"]), FeatureRecord.created_at < cutoff))
    await db.commit()
    return ok({"deleted": int(result.rowcount or 0)}, "已清理10天前的补发货日志")
