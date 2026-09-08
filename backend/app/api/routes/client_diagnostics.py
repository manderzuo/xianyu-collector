# -*- coding: utf-8 -*-
"""Client error-report upload and administrator review APIs."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Response

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.services.cloud_auth import CloudAuthError, cloud_auth_download, cloud_auth_request, cloud_auth_url

router = APIRouter(prefix="/api/v1/client-diagnostics", tags=["客户端诊断日志"])


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _token(user: dict[str, Any]) -> str:
    value = str(user.get("cloud_session_token") or "").strip()
    if not value:
        raise HTTPException(status_code=401, detail="云端登录状态已失效，请重新登录")
    return value


async def _cloud(action: str, payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if not cloud_auth_url():
        raise HTTPException(status_code=503, detail="云端诊断服务未启用")
    try:
        return await cloud_auth_request(action, payload, _token(user)) or {}
    except CloudAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _require_admin(user: dict[str, Any]) -> None:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可以查看客户端错误日志")


@router.post("/upload")
async def upload_diagnostic(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user)):
    """Upload a prebuilt, client-side-redacted ZIP archive.

    The cloud service derives the owner from the authenticated cloud session;
    a user_id supplied by a client is intentionally ignored.
    """
    result = await _cloud("diagnostics_upload", payload, user)
    return ok(result.get("report") or {}, result.get("message") or "诊断日志已上传")


@router.get("")
async def list_diagnostics(status: str | None = None, severity: str | None = None, search: str | None = None, limit: int = 50, offset: int = 0, user=Depends(get_current_user)):
    _require_admin(user)
    result = await _cloud("diagnostics_list", {"status": status or "", "severity": severity or "", "search": search or "", "limit": max(1, min(limit, 100)), "offset": max(0, offset)}, user)
    return ok({"items": result.get("items") or [], "total": result.get("total", 0), "limit": result.get("limit", limit), "offset": result.get("offset", offset)}, "诊断日志查询成功")


@router.get("/stats")
async def diagnostics_stats(user=Depends(get_current_user)):
    _require_admin(user)
    result = await _cloud("diagnostics_stats", {}, user)
    return ok(result.get("stats") or {}, "诊断日志统计查询成功")


@router.get("/{report_id}")
async def diagnostic_detail(report_id: int, user=Depends(get_current_user)):
    _require_admin(user)
    result = await _cloud("diagnostics_detail", {"report_id": report_id}, user)
    return ok(result.get("report") or {}, "诊断报告查询成功")


@router.post("/{report_id}/status")
async def update_diagnostic_status(report_id: int, payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user)):
    _require_admin(user)
    result = await _cloud("diagnostics_status", {"report_id": report_id, "status": payload.get("status"), "note": payload.get("note") or ""}, user)
    return ok(result.get("report") or {}, "诊断报告状态已更新")


@router.delete("/{report_id}")
async def delete_diagnostic(report_id: int, user=Depends(get_current_user)):
    _require_admin(user)
    result = await _cloud("diagnostics_delete", {"report_id": report_id}, user)
    return ok(result, "诊断报告已删除")


@router.get("/{report_id}/download")
async def download_diagnostic(report_id: int, user=Depends(get_current_user)):
    _require_admin(user)
    if not cloud_auth_url():
        raise HTTPException(status_code=503, detail="云端诊断服务未启用")
    try:
        data, filename = await cloud_auth_download("diagnostics_download", {"report_id": report_id}, _token(user))
    except CloudAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    safe_filename = filename.replace("\r", "").replace("\n", "").replace('"', "")
    return Response(content=data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{safe_filename}"', "Cache-Control": "no-store"})


__all__ = ["router"]
