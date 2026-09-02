# -*- coding: utf-8 -*-
"""管理员备份文件管理。

调度器负责生成备份，本路由只负责安全列出、下载和接收备份文件；上传的
文件会进入备份目录作为归档，不会在运行中的数据库上执行未经确认的恢复。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import error, ok
from common.config import settings

router = APIRouter(prefix="/api/v1/admin/backup", tags=["管理员备份"])
_MAX_UPLOAD_SIZE = 200 * 1024 * 1024


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _root() -> Path:
    root = Path(settings.backup_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_filename(value: str) -> str:
    name = Path(value or "").name
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,180}", name):
        return ""
    return name


def _files(root: Path) -> list[Path]:
    return sorted(
        (item for item in root.iterdir() if item.is_file() and item.name.endswith((".sql.gz", ".json.gz"))),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )


def _require(user: dict[str, Any]) -> None:
    if not _is_admin(user):
        raise PermissionError("仅管理员可以操作备份文件")


@router.get("/list")
async def list_backups(user=Depends(get_current_user)):
    try:
        _require(user)
    except PermissionError as exc:
        return error(str(exc), code="forbidden")
    rows = []
    for item in _files(_root()):
        stat = item.stat()
        rows.append({
            "filename": item.name,
            "name": item.name,
            "size": stat.st_size,
            "size_mb": round(stat.st_size / 1024 / 1024, 3),
            "modified_time": stat.st_mtime,
        })
    # 该接口保留旧页面使用的顶层 backups 字段，同时附带统一响应字段。
    return {"success": True, "code": "ok", "message": "查询成功", "backups": rows, "total": len(rows), "data": rows}


@router.get("/download")
async def download_backup(
    filename: str | None = Query(default=None),
    user=Depends(get_current_user),
):
    try:
        _require(user)
    except PermissionError as exc:
        return error(str(exc), code="forbidden")
    root = _root()
    selected = _safe_filename(filename or "")
    path = root / selected if selected else (_files(root)[0] if _files(root) else None)
    if path is None or not path.is_file() or root not in path.parents:
        return error("没有可下载的备份文件", code="backup_not_found")
    return FileResponse(path, media_type="application/gzip", filename=path.name)


@router.post("/upload")
async def upload_backup(
    backup_file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    try:
        _require(user)
    except PermissionError as exc:
        return error(str(exc), code="forbidden")
    original = _safe_filename(backup_file.filename or "")
    if not original.endswith((".sql.gz", ".json.gz")):
        return error("只支持 .sql.gz 或 .json.gz 备份文件", code="invalid_backup_file")
    content = await backup_file.read(_MAX_UPLOAD_SIZE + 1)
    if len(content) > _MAX_UPLOAD_SIZE:
        return error("备份文件不能超过200MB", code="backup_too_large")
    target = _root() / f"uploaded_{int(time.time())}_{original}"
    try:
        target.write_bytes(content)
    except OSError as exc:
        return error(f"备份文件保存失败：{str(exc)[:180]}", code="backup_upload_failed")
    return ok({"filename": target.name, "size": len(content), "restored": False}, "备份文件已归档；系统不会自动覆盖当前数据库")
