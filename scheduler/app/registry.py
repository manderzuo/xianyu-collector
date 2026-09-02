# -*- coding: utf-8 -*-
"""任务注册表与触发器。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from common.task_catalog import LEGACY_UNSUPPORTED_TASKS, TASK_ALIASES, TASK_CATALOG, canonical_task_name
from common.db.session import async_session_maker
from common.models import ScheduledTask
from scheduler.app.jobs.executor import execute


@dataclass(frozen=True)
class RegisteredTask:
    name: str
    display_name: str
    cron: str


REGISTRY = {name: RegisteredTask(name, display, cron) for name, display, cron in TASK_CATALOG}
LAST_RUN: dict[str, dict[str, Any]] = {}
RUNNING_TASKS: set[str] = set()
DEFAULT_DISABLED_TASKS = {"cleanup_browser_data", "close_notice"}


def interval_from_cron(cron: str) -> int:
    """将现有目录中的常用 cron 表达式转换为页面使用的秒数。"""
    value = str(cron or "").strip()
    if value == "0 * * * *":
        return 3600
    if value.startswith("0 */") and value.endswith(" * * *"):
        try:
            return max(60, int(value.split("*/", 1)[1].split(" ", 1)[0]) * 3600)
        except (TypeError, ValueError):
            pass
    if value.startswith("*/") and value.endswith(" * * * *"):
        try:
            return max(60, int(value.split("*/", 1)[1].split(" ", 1)[0]) * 60)
        except (TypeError, ValueError):
            pass
    if value.count(" ") == 4 and value.split(" ")[0] == "0":
        return 86400
    return 3600


async def sync_task_catalog() -> None:
    """把任务目录同步到数据库，供后台和运维查询。"""
    async with async_session_maker() as session:
        for name, display, cron in TASK_CATALOG:
            result = await session.execute(select(ScheduledTask).where(ScheduledTask.task_name == name))
            existing = result.scalar_one_or_none()
            if existing is None:
                session.add(ScheduledTask(task_name=name, display_name=display, cron_expr=cron, enabled=0 if name in DEFAULT_DISABLED_TASKS else 1, last_status="idle"))
            else:
                existing.display_name = display
                existing.cron_expr = cron
        await session.commit()


async def run_task(task_name: str, *, account_id: int | None = None, force: bool = False) -> dict[str, Any]:
    requested_task_name = str(task_name or "").strip()
    canonical_name = canonical_task_name(requested_task_name)
    if requested_task_name in LEGACY_UNSUPPORTED_TASKS:
        result = await execute(requested_task_name, account_id=account_id, force=force)
        LAST_RUN[requested_task_name] = result
        return result
    if canonical_name not in REGISTRY:
        return {"task_name": task_name, "status": "unknown"}
    RUNNING_TASKS.add(canonical_name)
    try:
        result = await execute(requested_task_name, account_id=account_id, force=force)
    except Exception as exc:  # 任务失败也必须回写状态，不能让触发接口无响应
        result = {
            "task_name": task_name,
            "status": "failed",
            "detail": f"任务执行异常：{str(exc)[:500]}",
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        RUNNING_TASKS.discard(canonical_name)
    LAST_RUN[requested_task_name] = result
    async with async_session_maker() as session:
        db_task_result = await session.execute(select(ScheduledTask).where(ScheduledTask.task_name == canonical_name))
        db_task = db_task_result.scalar_one_or_none()
        if db_task is None:
            registered = REGISTRY[canonical_name]
            db_task = ScheduledTask(task_name=canonical_name, display_name=registered.display_name, cron_expr=registered.cron, enabled=1)
            session.add(db_task)
        db_task.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
        # 旧表兼容：数据库字段为 String(16)，详细等待原因放在返回结果 detail 中。
        db_task.last_status = str(result.get("status", "unknown"))[:16]
        await session.commit()
    if requested_task_name in TASK_ALIASES:
        result = {**result, "requested_task_name": requested_task_name, "canonical_task_name": canonical_name}
    return result


async def run_all_once() -> None:
    await asyncio.gather(*(run_task(name) for name in REGISTRY))
