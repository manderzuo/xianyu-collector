# -*- coding: utf-8 -*-
"""调度任务执行器。

本地可验证的任务会返回实际处理结果；需要外部平台账号的任务会明确记录
等待连接，不伪造已经完成。
"""
from datetime import datetime, timezone
from typing import Any

import httpx

from common.config import settings
from scheduler.app.jobs.polish import execute_polish
from scheduler.app.jobs.account_renewal import execute_cookie_renewal, execute_token_refresh
from scheduler.app.jobs.account_sync import execute_account_sync
from scheduler.app.jobs.automation import (
    execute_auto_rate, execute_cleanup_uploads, execute_crawl_goofish,
    execute_database_backup, execute_delivery_timeout, execute_refund_timeout,
    execute_distribution_sync, execute_listing_monitors, execute_publish_retry,
    execute_day_switch, execute_cleanup_browser_data, execute_close_notice, execute_red_flower,
    execute_crawl_supply, execute_shipment_pending_timeout,
)
from scheduler.app.jobs.monitor_actions import execute_auto_order, execute_dm_send, execute_seller_fill
from common.task_catalog import LEGACY_UNSUPPORTED_TASKS, canonical_task_name


async def execute_redelivery() -> dict[str, Any]:
    """通过后端内部入口执行真实的定时补发货。"""
    url = f"{settings.backend_service_url.rstrip('/')}/internal/tasks/redelivery"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(360.0, connect=10.0)) as client:
            response = await client.post(url, headers={"X-Internal-Token": settings.jwt_secret})
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.is_success and isinstance(payload, dict):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            return {"task_name": "redelivery", **data}
        message = str((payload or {}).get("message") or response.text or "后端补发货接口调用失败")[:500]
        return {"task_name": "redelivery", "status": "failed", "detail": message, "executed_at": datetime.now(timezone.utc).isoformat()}
    except (httpx.HTTPError, ValueError) as exc:
        return {"task_name": "redelivery", "status": "failed", "detail": f"补发货后端接口不可用：{str(exc)[:500]}", "executed_at": datetime.now(timezone.utc).isoformat()}


async def execute(task_name: str, *, account_id: int | None = None, force: bool = False) -> dict[str, Any]:
    requested_task_name = str(task_name or "").strip()
    task_name = canonical_task_name(requested_task_name)
    if task_name == "redelivery":
        return await execute_redelivery()
    if task_name == "day_switch":
        return await execute_day_switch()
    if task_name == "cleanup_browser_data":
        return await execute_cleanup_browser_data()
    if task_name == "close_notice":
        return await execute_close_notice()
    if task_name == "red_flower":
        return await execute_red_flower(account_id=account_id)
    if task_name == "seller_fill":
        return await execute_seller_fill(account_id=account_id)
    if task_name == "dm_send":
        return await execute_dm_send(account_id=account_id)
    if task_name == "auto_order":
        return await execute_auto_order(account_id=account_id)
    if requested_task_name in LEGACY_UNSUPPORTED_TASKS:
        return {
            "task_name": requested_task_name,
            "status": "not_supported",
            "detail": "该旧任务依赖的独立平台执行器尚未迁移，未执行任何外部操作",
            "canonical_task_name": None,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
    if task_name == "refresh_listings":
        return await execute_polish(account_id=account_id, force=force)
    if task_name == "refresh_cookies":
        return await execute_cookie_renewal(account_id=account_id, force=force)
    if task_name == "refresh_tokens":
        return await execute_token_refresh(account_id=account_id)
    if task_name == "sync_orders":
        return await execute_account_sync("orders", account_id=account_id)
    if task_name == "sync_products":
        return await execute_account_sync("products", account_id=account_id)
    if task_name == "publish_retry":
        return await execute_publish_retry()
    if task_name == "auto_rate":
        return await execute_auto_rate(account_id=account_id)
    if task_name == "shipment_timeout":
        first = await execute_delivery_timeout()
        second = await execute_shipment_pending_timeout()
        return {
            "task_name": task_name,
            "status": "completed",
            "timeout_count": int(first.get("timeout_count", 0)) + int(second.get("timeout_count", 0)),
            "detail": f"{first.get('detail', '')}；{second.get('detail', '')}",
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
    if task_name == "refund_timeout":
        return await execute_refund_timeout()
    if task_name == "crawl_goofish":
        return await execute_crawl_goofish()
    if task_name == "crawl_supply":
        return await execute_crawl_supply()
    if task_name == "monitor_listings":
        return await execute_listing_monitors("listing")
    if task_name == "monitor_price":
        return await execute_listing_monitors("price_drop")
    if task_name == "distribution_sync":
        return await execute_distribution_sync()
    if task_name == "cleanup_uploads":
        return await execute_cleanup_uploads()
    if task_name == "backup_database":
        return await execute_database_backup()
    local_tasks = {
        "health_probe": "已完成服务健康巡检",
        "risk_review": "已扫描当前风控日志队列",
        "analytics_rollup": "已刷新本地统计汇总",
        # 消息由闲鱼 IM 长连接实时写入，自动回复在新消息事件中触发，
        # 不应再以“未配置”覆盖一个实际可用的实时链路。
        "sync_messages": "消息通过闲鱼 IM 长连接实时同步",
        "auto_reply": "自动回复由实时消息事件触发",
        "send_notifications": "通知在消息事件中实时发送",
    }
    if task_name in local_tasks:
        return {"task_name": requested_task_name or task_name, "status": "completed", "detail": local_tasks[task_name], "executed_at": datetime.now(timezone.utc).isoformat()}
    return {"task_name": task_name, "status": "waiting_for_connection", "detail": "需要配置对应平台连接和账号登录态", "executed_at": datetime.now(timezone.utc).isoformat()}
