# -*- coding: utf-8 -*-
"""独立调度服务：任务目录、手动触发与 APScheduler 生命周期。"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo
from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.config import settings
from common.db.session import async_session_maker, get_session, init_db
from common.models import ScheduledTask
from common.task_catalog import LEGACY_TASK_NAMES, TASK_CATALOG, canonical_task_name
from scheduler.app.registry import REGISTRY, LAST_RUN, RUNNING_TASKS, interval_from_cron, run_task, sync_task_catalog

scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


async def _task_rows() -> dict[str, tuple[bool, int | None]]:
    """读取任务开关和自定义间隔。"""
    rows: dict[str, tuple[bool, int | None]] = {}
    async with async_session_maker() as session:
        rows = {
            item.task_name: (bool(item.enabled), int(item.interval_seconds) if item.interval_seconds else None)
            for item in (await session.execute(select(ScheduledTask))).scalars().all()
        }
    return rows


def _install_jobs(task_rows: dict[str, tuple[bool, int | None]]) -> None:
    """按数据库配置重建 APScheduler 任务。可在进程内安全重复调用。"""
    for job in scheduler.get_jobs():
        scheduler.remove_job(job.id)

    for task in REGISTRY.values():
        run_immediately = task.name in {"refresh_cookies", "refresh_tokens"}
        job_kwargs = {
            "args": [task.name],
            "id": task.name,
            "replace_existing": True,
            "coalesce": True,
            "max_instances": 1,
        }
        default_enabled = task.name not in {"cleanup_browser_data", "close_notice"}
        enabled, interval_seconds = task_rows.get(task.name, (default_enabled, None))
        if run_immediately:
            job_kwargs["next_run_time"] = datetime.now(ZoneInfo("Asia/Shanghai"))
        trigger = IntervalTrigger(seconds=interval_seconds, timezone="Asia/Shanghai") if interval_seconds else CronTrigger.from_crontab(task.cron, timezone="Asia/Shanghai")
        scheduler.add_job(
            run_task,
            trigger,
            **job_kwargs,
        )
        if not enabled:
            scheduler.pause_job(task.name)


async def _reload_jobs() -> int:
    """重新读取任务配置并重建调度任务，返回任务总数。"""
    await sync_task_catalog()
    _install_jobs(await _task_rows())
    return len(REGISTRY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_db()
        await sync_task_catalog()
    except Exception as exc:
        # 调度服务仍可提供健康检查，任务触发时会返回明确的数据库错误。
        import logging
        logging.getLogger("xr.scheduler").warning("database initialization skipped: %s", exc)
    task_rows: dict[str, tuple[bool, int | None]] = {}
    try:
        task_rows = await _task_rows()
    except Exception as exc:
        logging.getLogger("xr.scheduler").warning("task configuration load skipped: %s", exc)
    _install_jobs(task_rows)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title=f"{settings.brand_name} Scheduler", version="1.0.6", lifespan=lifespan)


@app.middleware("http")
async def protect_internal_routes(request, call_next):
    """调度控制接口只允许受信任服务调用。"""
    if request.url.path.startswith("/internal/"):
        token = request.headers.get("X-Internal-Token", "")
        if not token or not secrets.compare_digest(token, settings.jwt_secret):
            return JSONResponse(status_code=401, content={"detail": "内部调用凭证无效"})
    return await call_next(request)


@app.get("/health", tags=["系统"])
async def health():
    return {"success": True, "code": "ok", "message": "操作成功", "data": {"service": "scheduler", "status": "running", "task_count": len(REGISTRY)}}


@app.post("/internal/reload", tags=["系统"])
async def reload_scheduler():
    """在不依赖 Docker Socket 的情况下重新加载调度任务。"""
    try:
        if not scheduler.running:
            scheduler.start()
        task_count = await _reload_jobs()
        return {
            "success": True,
            "code": "ok",
            "message": "定时任务服务已重新加载",
            "data": {"service": "scheduler", "status": "running", "mode": "in_process_reload", "task_count": task_count},
        }
    except Exception as exc:
        logging.getLogger("xr.scheduler").exception("scheduler reload failed: %s", exc)
        return {
            "success": False,
            "code": "reload_failed",
            "message": f"定时任务服务重新加载失败：{str(exc)[:300]}",
            "data": {"service": "scheduler", "status": "failed"},
        }


@app.get("/api/v1/scheduled-tasks", tags=["定时任务"])
async def list_tasks(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(ScheduledTask).order_by(ScheduledTask.id.asc()))
    items = [{
        "id": item.id,
        "task_code": item.task_name,
        "task_name": item.display_name,
        "name": item.task_name,
        "display_name": item.display_name,
        "cron": item.cron_expr,
        "interval_seconds": int(item.interval_seconds or interval_from_cron(item.cron_expr)),
        "enabled": bool(item.enabled),
        "description": item.display_name,
        "task_running": item.task_name in RUNNING_TASKS,
        "last_status": item.last_status or "idle",
        "last_run_at": item.last_run_at.isoformat() if item.last_run_at else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    } for item in result.scalars().all()]
    return {"success": True, "code": "ok", "message": "操作成功", "data": {"items": items, "total": len(items)}}


@app.put("/api/v1/scheduled-tasks/{task_name}", tags=["定时任务"])
async def update_task(
    task_name: str,
    interval_seconds: int | None = None,
    enabled: bool | None = None,
    session: AsyncSession = Depends(get_session),
):
    task_name = canonical_task_name(task_name)
    if task_name not in REGISTRY:
        raise HTTPException(status_code=404, detail="任务不存在")
    if interval_seconds is not None and not 1 <= interval_seconds <= 86400:
        raise HTTPException(status_code=422, detail="执行间隔必须在1到86400秒之间")
    item = (await session.execute(select(ScheduledTask).where(ScheduledTask.task_name == task_name))).scalar_one_or_none()
    if item is None:
        registered = REGISTRY[task_name]
        item = ScheduledTask(task_name=task_name, display_name=registered.display_name, cron_expr=registered.cron, enabled=1)
        session.add(item)
    if interval_seconds is not None:
        item.interval_seconds = interval_seconds
    if enabled is not None:
        item.enabled = 1 if enabled else 0
    await session.commit()
    job = scheduler.get_job(task_name)
    if job is not None:
        if interval_seconds is not None:
            scheduler.reschedule_job(task_name, trigger=IntervalTrigger(seconds=interval_seconds, timezone="Asia/Shanghai"))
        if enabled is False:
            scheduler.pause_job(task_name)
        elif enabled is True:
            scheduler.resume_job(task_name)
    await session.refresh(item)
    return {"success": True, "code": "ok", "message": "定时任务配置已更新", "data": {
        "id": item.id, "task_code": item.task_name, "task_name": item.display_name,
        "interval_seconds": int(item.interval_seconds or interval_from_cron(item.cron_expr)),
        "enabled": bool(item.enabled), "description": item.display_name,
        "task_running": item.task_name in RUNNING_TASKS,
        "last_status": item.last_status or "idle",
        "last_run_at": item.last_run_at.isoformat() if item.last_run_at else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }}


@app.post("/api/v1/scheduled-tasks/{task_name}/trigger", tags=["定时任务"])
async def trigger_task(task_name: str, payload: dict | None = Body(default=None)):
    if task_name not in REGISTRY and task_name not in LEGACY_TASK_NAMES:
        raise HTTPException(status_code=404, detail="任务不存在")
    data = payload or {}
    account_id = data.get("account_id")
    if account_id is not None:
        try:
            account_id = int(account_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="账号ID无效") from exc
    result = await run_task(task_name, account_id=account_id, force=bool(data.get("force", False)))
    success = result.get("status") in {"completed", "success", "skipped"}
    return {"success": success, "code": "ok" if success else result.get("status", "failed"), "message": "任务执行完成" if success else result.get("detail", "任务未执行"), "data": result}
