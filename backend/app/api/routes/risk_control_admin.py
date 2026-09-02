"""管理员清理风控日志接口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models import RiskControlLog

router = APIRouter(prefix="/api/v1/admin", tags=["管理员风控日志"])


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


@router.delete("/risk-control-logs")
async def clear_risk_logs(cookie_id: str | None = None, processing_status: str | None = Query(None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    if not _is_admin(user):
        raise HTTPException(403, "仅管理员可以清理风控日志")
    statement = delete(RiskControlLog)
    if cookie_id:
        try:
            statement = statement.where(RiskControlLog.account_id == int(cookie_id))
        except ValueError as exc:
            raise HTTPException(422, "账号ID无效") from exc
    if processing_status:
        statement = statement.where(RiskControlLog.processing_status == processing_status)
    result = await db.execute(statement)
    await db.commit()
    return ok({"deleted": int(result.rowcount or 0)}, "风控日志已清理")
