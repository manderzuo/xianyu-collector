# -*- coding: utf-8 -*-
"""数据库备份日志与安全下载接口。"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.config import settings
from common.db.session import get_session
from common.models import FeatureRecord

router = APIRouter(prefix="/api/v1/db-backup-logs", tags=["数据库备份"])


def _admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _row(item: FeatureRecord) -> dict[str, Any]:
    payload = dict(item.payload or {})
    payload.update({"id": item.id, "status": item.status, "created_at": item.created_at, "updated_at": item.updated_at})
    return payload


def _require_admin(user: dict[str, Any]) -> None:
    if not _admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可以查看数据库备份")


@router.get("")
@router.get("/")
async def list_backup_logs(
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), status: str | None = Query(None),
    start_date: str | None = Query(None), end_date: str | None = Query(None),
    user=Depends(get_current_user), db: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    statement = select(FeatureRecord).where(FeatureRecord.feature == "db-backup-logs")
    if status:
        statement = statement.where(FeatureRecord.status == status)
    try:
        if start_date:
            statement = statement.where(FeatureRecord.created_at >= datetime.fromisoformat(start_date))
        if end_date:
            statement = statement.where(FeatureRecord.created_at < datetime.fromisoformat(end_date) + timedelta(days=1))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="日期格式无效") from exc
    rows = list((await db.execute(statement.order_by(FeatureRecord.id.desc()))).scalars().all())
    return ok({"items": [_row(item) for item in rows[offset:offset + limit]], "total": len(rows), "offset": offset, "limit": limit}, "查询成功")


@router.get("/{log_id}/download")
async def download_backup(log_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _require_admin(user)
    item = (await db.execute(select(FeatureRecord).where(FeatureRecord.id == log_id, FeatureRecord.feature == "db-backup-logs"))).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="备份记录不存在")
    payload = item.payload or {}
    filename = str(payload.get("file_name") or "").strip()
    if item.status != "success" or not filename or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="该备份文件不可下载")
    root = Path(settings.backup_dir).resolve()
    path = (root / filename).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="备份文件不存在")
    return FileResponse(path, media_type="application/gzip", filename=filename)


__all__ = ["router"]
