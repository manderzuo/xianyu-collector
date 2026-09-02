"""风控日志与当日统计。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models import RiskControlLog, SystemSetting

router = APIRouter(prefix="/api/v1/risk-control-logs", tags=["风控日志"])
BJ = ZoneInfo("Asia/Shanghai")


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _row(item: RiskControlLog) -> dict[str, Any]:
    return {
        "id": item.id,
        "cookie_id": str(item.account_id) if item.account_id is not None else "",
        "account_id": item.account_id,
        "event_type": item.event_type,
        "risk_type": item.event_type,
        "event_description": item.event_description or "",
        "processing_result": item.processing_result or "",
        "processing_status": item.processing_status,
        "captcha_engine": item.captcha_engine,
        "call_type": item.call_type,
        "call_user": item.call_user,
        "error_message": item.error_message,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _day_range() -> tuple[datetime, datetime]:
    today = datetime.now(BJ).date()
    start = datetime.combine(today, time.min)
    end = start + timedelta(days=1)
    return start, end


def _filters(statement, *, user: dict[str, Any], cookie_id: str | None = None, start_date: str | None = None, end_date: str | None = None, processing_status: str | None = None, call_type: str | None = None, call_user: str | None = None):
    if not _is_admin(user):
        statement = statement.where(RiskControlLog.owner_id == _uid(user))
    if cookie_id:
        try:
            statement = statement.where(RiskControlLog.account_id == int(cookie_id))
        except ValueError:
            statement = statement.where(RiskControlLog.account_id == -1)
    try:
        if start_date:
            statement = statement.where(RiskControlLog.created_at >= datetime.combine(date.fromisoformat(start_date), time.min))
        if end_date:
            statement = statement.where(RiskControlLog.created_at < datetime.combine(date.fromisoformat(end_date) + timedelta(days=1), time.min))
    except ValueError as exc:
        raise HTTPException(422, "日期格式必须为YYYY-MM-DD") from exc
    if processing_status:
        statement = statement.where(RiskControlLog.processing_status == processing_status)
    if call_type:
        statement = statement.where(RiskControlLog.call_type == call_type)
    if call_user:
        statement = statement.where(RiskControlLog.call_user.ilike(f"%{call_user}%"))
    return statement


@router.get("")
@router.get("/")
async def list_risk_logs(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    cookie_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    processing_status: str | None = None,
    call_type: str | None = None,
    call_user: str | None = None,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    base = _filters(select(RiskControlLog), user=user, cookie_id=cookie_id, start_date=start_date, end_date=end_date, processing_status=processing_status, call_type=call_type, call_user=call_user)
    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
    rows = list((await db.execute(base.order_by(RiskControlLog.id.desc()).offset(offset).limit(limit))).scalars().all())
    values = [_row(item) for item in rows]
    return ok({"items": values, "list": values, "total": total, "limit": limit, "offset": offset})


@router.get("/today-success-rate")
async def today_success_rate(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    start, end = _day_range()
    base = select(RiskControlLog).where(RiskControlLog.created_at >= start, RiskControlLog.created_at < end)
    if not _is_admin(user):
        base = base.where(RiskControlLog.owner_id == _uid(user))
    rows = list((await db.execute(base)).scalars().all())

    def stats(values: list[RiskControlLog]) -> tuple[int, int, float]:
        finished = [item for item in values if item.processing_status != "processing"]
        success = sum(item.processing_status == "success" for item in finished)
        total = len(finished)
        return total, success, round(success * 100 / total, 1) if total else 0

    total, success, rate = stats(rows)
    local_rows = [item for item in rows if (item.call_type or "local") == "local"]
    remote_rows = [item for item in rows if item.call_type == "remote"]
    local_total, local_success, local_rate = stats(local_rows)
    remote_total, remote_success, remote_rate = stats(remote_rows)
    processing = [item for item in rows if item.processing_status == "processing"]
    local_processing = sum((item.call_type or "local") == "local" for item in processing)
    remote_processing = sum(item.call_type == "remote" for item in processing)
    return ok({
        "date": datetime.now(BJ).date().isoformat(),
        "total": total, "success": success, "rate": rate,
        "local_total": local_total, "local_success": local_success, "local_rate": local_rate,
        "remote_total": remote_total, "remote_success": remote_success, "remote_rate": remote_rate,
        "processing": len(processing), "local_processing": local_processing, "remote_processing": remote_processing,
    })


@router.get("/local-slider-config")
async def get_local_slider_config(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = (await db.execute(select(SystemSetting).where(SystemSetting.setting_key == "risk.local_slider_enabled"))).scalar_one_or_none()
    enabled = str(item.setting_value if item else "true").lower() not in {"0", "false", "no", "off"}
    return ok({"enabled": enabled})


@router.put("/local-slider-config")
async def update_local_slider_config(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    if not _is_admin(user):
        raise HTTPException(403, "仅管理员可以修改本机风控配置")
    enabled = bool(payload.get("enabled", True))
    item = (await db.execute(select(SystemSetting).where(SystemSetting.setting_key == "risk.local_slider_enabled"))).scalar_one_or_none()
    if item is None:
        item = SystemSetting(setting_key="risk.local_slider_enabled", setting_value="true" if enabled else "false")
        db.add(item)
    else:
        item.setting_value = "true" if enabled else "false"
    await db.commit()
    return ok({"enabled": enabled}, "本机风控配置已更新")
