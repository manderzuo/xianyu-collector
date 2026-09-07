# -*- coding: utf-8 -*-
import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from common.task_catalog import LEGACY_TASK_NAMES, TASK_CATALOG, canonical_task_name
from common.config import settings
from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok

router = APIRouter(prefix="/api/v1/scheduled-tasks", tags=["定时任务"], dependencies=[Depends(get_current_user)])
admin_router = APIRouter(prefix="/api/v1/admin/scheduled-tasks", tags=["管理端定时任务"], dependencies=[Depends(get_current_user)])

TASKS = TASK_CATALOG


def _require_admin(user: dict) -> None:
    if str(user.get("role") or "").lower() not in {"admin", "administrator"} and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可以管理定时任务")


@router.get("")
@router.get("/")
async def list_tasks():
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{settings.scheduler_service_url.rstrip('/')}/api/v1/scheduled-tasks")
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="定时任务服务暂不可用") from exc


@router.post("/{task_name}/trigger")
async def trigger_task(task_name: str, payload: dict | None = Body(default=None)):
    known = {item[0] for item in TASKS} | set(LEGACY_TASK_NAMES)
    if task_name not in known:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        # Cookie续期可能需要启动持久化浏览器并等待页面完成登录态同步。
        # 擦亮遇到 Session 过期时会先执行一次账号续期再重试，允许浏览器
        # 续期链路有足够时间完成，避免后端在 30 秒时提前断开请求。
        request_timeout = 360 if canonical_task_name(task_name) in {"refresh_cookies", "refresh_tokens", "refresh_listings"} else 30
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            response = await client.post(
                f"{settings.scheduler_service_url.rstrip('/')}/api/v1/scheduled-tasks/{task_name}/trigger",
                json=payload or {},
            )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="定时任务服务暂不可用") from exc


async def _scheduler_request(method: str, path: str, **kwargs):
    try:
        async with httpx.AsyncClient(timeout=360 if method == "POST" else 30) as client:
            response = await client.request(method, f"{settings.scheduler_service_url.rstrip('/')}{path}", **kwargs)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="定时任务服务暂不可用") from exc


@admin_router.get("")
@admin_router.get("/")
async def list_admin_tasks(user=Depends(get_current_user)):
    _require_admin(user)
    return await _scheduler_request("GET", "/api/v1/scheduled-tasks")


@admin_router.put("/{task_code}")
async def update_admin_task(
    task_code: str,
    interval_seconds: int | None = Query(default=None, ge=1, le=86400),
    enabled: bool | None = Query(default=None),
    user=Depends(get_current_user),
):
    _require_admin(user)
    canonical = canonical_task_name(task_code)
    if canonical not in {item[0] for item in TASKS}:
        raise HTTPException(status_code=409, detail="该旧任务没有可迁移的调度配置")
    params = {}
    if interval_seconds is not None:
        params["interval_seconds"] = interval_seconds
    if enabled is not None:
        params["enabled"] = enabled
    return await _scheduler_request("PUT", f"/api/v1/scheduled-tasks/{canonical}", params=params)


@admin_router.post("/{task_code}/trigger")
async def trigger_admin_task(task_code: str, payload: dict | None = Body(default=None), user=Depends(get_current_user)):
    _require_admin(user)
    if task_code not in {item[0] for item in TASKS} and task_code not in LEGACY_TASK_NAMES:
        raise HTTPException(status_code=404, detail="任务不存在")
    return await _scheduler_request("POST", f"/api/v1/scheduled-tasks/{task_code}/trigger", json=payload or {})
