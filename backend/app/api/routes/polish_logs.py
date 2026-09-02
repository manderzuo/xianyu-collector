"""自动擦亮批次和明细接口。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models import PolishLog

router = APIRouter(prefix="/api/v1/admin", tags=["自动擦亮"])


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _guard(user: dict[str, Any]) -> None:
    if not _is_admin(user):
        raise HTTPException(403, "仅管理员可以查看自动擦亮日志")


def _bounds(start_date: str | None, end_date: str | None):
    try:
        start = datetime.combine(date.fromisoformat(start_date), time.min) if start_date else None
        end = datetime.combine(date.fromisoformat(end_date) + timedelta(days=1), time.min) if end_date else None
        return start, end
    except ValueError as exc:
        raise HTTPException(422, "日期格式必须为YYYY-MM-DD") from exc


def _summary(batch_id: str, rows: list[PolishLog]) -> dict[str, Any]:
    total = len(rows)
    success = sum(row.status == "success" for row in rows)
    skipped = sum(row.status == "skipped" for row in rows)
    failed = sum(row.status == "failed" for row in rows)
    executed = min((row.created_at for row in rows if row.created_at), default=None)
    return {
        "batch_id": batch_id,
        "executed_at": executed.isoformat() if executed else None,
        "total_items": total,
        "success_count": success,
        "skipped_count": skipped,
        "failed_count": failed,
    }


@router.get("/polish-batches")
async def list_polish_batches(start_date: str | None = None, end_date: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _guard(user)
    start, end = _bounds(start_date, end_date)
    statement = select(PolishLog).order_by(PolishLog.created_at.desc())
    if start:
        statement = statement.where(PolishLog.created_at >= start)
    if end:
        statement = statement.where(PolishLog.created_at < end)
    rows = list((await db.execute(statement)).scalars().all())
    grouped: dict[str, list[PolishLog]] = {}
    for row in rows:
        grouped.setdefault(row.batch_id, []).append(row)
    batches = [_summary(batch_id, values) for batch_id, values in grouped.items()]
    total = len(batches)
    sliced = batches[(page - 1) * page_size : page * page_size]
    return ok({"items": sliced, "total": total, "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size})


@router.get("/polish-batches/{batch_id}")
async def polish_batch_detail(batch_id: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _guard(user)
    rows = list((await db.execute(select(PolishLog).where(PolishLog.batch_id == batch_id).order_by(PolishLog.id.asc()))).scalars().all())
    summary = _summary(batch_id, rows)
    summary["logs"] = [{"id": row.id, "batch_id": row.batch_id, "account_id": row.account_id, "item_id": row.item_id, "status": row.status, "error_message": row.error_message, "created_at": row.created_at.isoformat() if row.created_at else None} for row in rows]
    return ok(summary)


@router.delete("/polish-logs/clear")
async def clear_polish_logs(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _guard(user)
    result = await db.execute(delete(PolishLog).where(PolishLog.created_at < datetime.now() - timedelta(days=10)))
    await db.commit()
    return ok({"deleted": int(result.rowcount or 0)}, "已清理10天前的擦亮日志")
