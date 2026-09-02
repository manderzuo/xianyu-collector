"""自动求小红花和关闭通知的批次日志接口。"""
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

router = APIRouter(prefix="/api/v1/admin", tags=["调度批次日志"])


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _guard(user: dict[str, Any]) -> None:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可以查看调度日志")


def _bounds(start_date: str | None, end_date: str | None) -> tuple[datetime | None, datetime | None]:
    try:
        start = datetime.combine(date.fromisoformat(start_date), time.min) if start_date else None
        end = datetime.combine(date.fromisoformat(end_date) + timedelta(days=1), time.min) if end_date else None
        return start, end
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="日期格式必须为YYYY-MM-DD") from exc


def _row_view(row: FeatureRecord, *, kind: str) -> dict[str, Any]:
    payload = dict(row.payload or {})
    value = {
        "id": row.id,
        "batch_id": str(payload.get("batch_id") or ""),
        "account_id": str(payload.get("account_id") or ""),
        "status": str(payload.get("status") or row.status),
        "error_message": payload.get("error_message"),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if kind == "red-flower-logs":
        value["order_no"] = str(payload.get("order_no") or "")
    return value


async def _list(kind: str, start_date: str | None, end_date: str | None, page: int, page_size: int, user: dict[str, Any], db: AsyncSession):
    _guard(user)
    start, end = _bounds(start_date, end_date)
    statement = select(FeatureRecord).where(FeatureRecord.feature == kind).order_by(FeatureRecord.created_at.asc(), FeatureRecord.id.asc())
    if start:
        statement = statement.where(FeatureRecord.created_at >= start)
    if end:
        statement = statement.where(FeatureRecord.created_at < end)
    rows = list((await db.execute(statement)).scalars().all())
    grouped: dict[str, list[FeatureRecord]] = {}
    for row in rows:
        batch_id = str((row.payload or {}).get("batch_id") or "").strip()
        if batch_id:
            grouped.setdefault(batch_id, []).append(row)
    batches = []
    for batch_id, batch_rows in grouped.items():
        successes = sum(1 for row in batch_rows if str((row.payload or {}).get("status") or row.status) == "success")
        item: dict[str, Any] = {
            "batch_id": batch_id,
            "executed_at": batch_rows[0].created_at.isoformat() if batch_rows[0].created_at else None,
            "success_count": successes,
            "failed_count": len(batch_rows) - successes,
        }
        item["total_orders" if kind == "red-flower-logs" else "total_accounts"] = len(batch_rows)
        batches.append(item)
    batches.sort(key=lambda item: item.get("executed_at") or "", reverse=True)
    total = len(batches)
    return ok({"items": batches[(page - 1) * page_size: page * page_size], "total": total, "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size})


async def _detail(kind: str, batch_id: str, user: dict[str, Any], db: AsyncSession):
    _guard(user)
    rows = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == kind).order_by(FeatureRecord.created_at.asc(), FeatureRecord.id.asc()))).scalars().all())
    batch_rows = [row for row in rows if str((row.payload or {}).get("batch_id") or "") == batch_id]
    if not batch_rows:
        raise HTTPException(status_code=404, detail="调度批次不存在")
    successes = sum(1 for row in batch_rows if str((row.payload or {}).get("status") or row.status) == "success")
    data: dict[str, Any] = {
        "batch_id": batch_id,
        "executed_at": batch_rows[0].created_at.isoformat() if batch_rows[0].created_at else None,
        "success_count": successes,
        "failed_count": len(batch_rows) - successes,
        "logs": [_row_view(row, kind=kind) for row in batch_rows],
    }
    data["total_orders" if kind == "red-flower-logs" else "total_accounts"] = len(batch_rows)
    return ok(data)


@router.get("/red-flower-batches")
async def list_red_flower_batches(start_date: str | None = None, end_date: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return await _list("red-flower-logs", start_date, end_date, page, page_size, user, db)


@router.get("/red-flower-batches/{batch_id}")
async def red_flower_batch_detail(batch_id: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return await _detail("red-flower-logs", batch_id, user, db)


@router.delete("/red-flower-logs/clear")
async def clear_red_flower_logs(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _guard(user)
    result = await db.execute(delete(FeatureRecord).where(FeatureRecord.feature == "red-flower-logs", FeatureRecord.created_at < datetime.now() - timedelta(days=10)))
    await db.commit()
    return ok({"deleted": int(result.rowcount or 0)}, "已清理10天前的求小红花日志")


@router.get("/close-notice-batches")
async def list_close_notice_batches(start_date: str | None = None, end_date: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return await _list("close-notice-logs", start_date, end_date, page, page_size, user, db)


@router.get("/close-notice-batches/{batch_id}")
async def close_notice_batch_detail(batch_id: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return await _detail("close-notice-logs", batch_id, user, db)


@router.delete("/close-notice-logs/clear")
async def clear_close_notice_logs(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _guard(user)
    result = await db.execute(delete(FeatureRecord).where(FeatureRecord.feature == "close-notice-logs", FeatureRecord.created_at < datetime.now() - timedelta(days=10)))
    await db.commit()
    return ok({"deleted": int(result.rowcount or 0)}, "已清理10天前的消息通知关闭日志")
