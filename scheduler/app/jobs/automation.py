# -*- coding: utf-8 -*-
"""剩余定时任务的真实执行实现。"""
from __future__ import annotations

import asyncio
import gzip
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import func, select, text

from common.config import settings
from common.db.session import async_session_maker
from common.models import (
    Account, AccountContent, AccountCookie, CardDeliveryRecord, FeatureRecord, GoofishCrawlJob, GoofishCrawlResult,
    Order, RefundCase,
)
from common.services.auto_rate import run_rate_batch
from common.services.goofish_client import GoofishClient
from common.services.goofish_publish import GoofishPublishError, detect_publish_capability, publish_item
from common.services.xianyu_platform import close_account_notice, request_red_flower
from common.db.redis_client import get_redis_client


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def execute_auto_rate(account_id: int | None = None) -> dict[str, Any]:
    async with async_session_maker() as session:
        statement = select(Account).where(Account.status == "active", Account.cookie.isnot(None), Account.cookie != "")
        if account_id is not None:
            statement = statement.where(Account.id == account_id)
        accounts = list((await session.execute(statement.order_by(Account.id.asc()))).scalars().all())
        result = await run_rate_batch(session, accounts, scheduled_only=True)
    failed_count = int(result.get("failed_count") or 0)
    return {"task_name": "auto_rate", "status": "partial" if failed_count else "completed", "detail": f"自动评价处理 {result['total_orders']} 笔订单，成功 {result['success_count']} 笔", **result, "executed_at": _now().isoformat()}


async def execute_day_switch() -> dict[str, Any]:
    """按北京时间切换平台日，并重置当前商品的本地擦亮标记。"""
    current_day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    client = get_redis_client()
    lock_key = "platform:day:switch:lock"
    acquired = False
    try:
        acquired = bool(await client.set(lock_key, current_day, nx=True, ex=90))
        if not acquired:
            return {"task_name": "day_switch", "status": "skipped", "reset_count": 0, "detail": "平台日切换正在由其他调度进程执行", "executed_at": _now().isoformat()}
        previous_day = await client.get("platform:day")
        if str(previous_day or "") == current_day:
            return {"task_name": "day_switch", "status": "skipped", "reset_count": 0, "detail": "平台日未变化", "platform_day": current_day, "executed_at": _now().isoformat()}
        async with async_session_maker() as session:
            items = list((await session.execute(select(AccountContent).where(AccountContent.content_type == "product"))).scalars().all())
            reset_count = 0
            for item in items:
                payload = dict(item.payload or {})
                if payload.get("is_polished") is not False:
                    payload["is_polished"] = False
                    item.payload = payload
                    reset_count += 1
            await session.commit()
        await client.set("platform:day", current_day)
        return {"task_name": "day_switch", "status": "completed", "reset_count": reset_count, "platform_day": current_day, "previous_platform_day": previous_day, "detail": f"已重置 {reset_count} 个商品的擦亮状态", "executed_at": _now().isoformat()}
    except Exception as exc:
        return {"task_name": "day_switch", "status": "failed", "reset_count": 0, "detail": f"平台日切换失败：{str(exc)[:500]}", "executed_at": _now().isoformat()}
    finally:
        try:
            if acquired:
                await client.delete(lock_key)
            await client.aclose()
        except Exception:
            pass


async def execute_cleanup_browser_data() -> dict[str, Any]:
    """把禁用超过十天的账号交给 websocket 服务清理浏览器 profile。"""
    cutoff = _now() - timedelta(days=10)
    async with async_session_maker() as session:
        accounts = list((await session.execute(select(Account).where(Account.status == "disabled", Account.updated_at <= cutoff))).scalars().all())
    account_ids = [str(account.id) for account in accounts]
    if not account_ids:
        return {"task_name": "cleanup_browser_data", "status": "completed", "cleaned_count": 0, "skipped_count": 0, "failed_count": 0, "detail": "没有需要清理的禁用账号", "executed_at": _now().isoformat()}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
            response = await client.post(
                f"{settings.websocket_service_url.rstrip('/')}/internal/browser-data/cleanup",
                json={"account_ids": account_ids},
                headers={"X-Internal-Token": settings.jwt_secret},
            )
        payload = response.json() if response.content else {}
        if not response.is_success or not payload.get("success"):
            return {"task_name": "cleanup_browser_data", "status": "failed", "detail": str(payload.get("message") or response.text or "浏览器数据清理失败")[:500], "executed_at": _now().isoformat()}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return {"task_name": "cleanup_browser_data", "status": "completed", **data, "detail": f"已处理 {len(account_ids)} 个禁用账号的浏览器数据", "executed_at": _now().isoformat()}
    except (httpx.HTTPError, ValueError) as exc:
        return {"task_name": "cleanup_browser_data", "status": "failed", "detail": f"浏览器数据清理服务不可用：{str(exc)[:500]}", "executed_at": _now().isoformat()}


async def execute_close_notice() -> dict[str, Any]:
    """对启用账号调用平台关闭消息通知接口，并记录逐账号结果。"""
    batch_id = uuid4().hex
    success_count = failed_count = 0
    async with async_session_maker() as session:
        accounts = list((await session.execute(select(Account).where(Account.status == "active", Account.cookie.isnot(None), Account.cookie != "").order_by(Account.id.asc()))).scalars().all())
        for account in accounts:
            message = None
            latest_cookie = str(account.cookie or "")
            status = "success"
            try:
                result = await close_account_notice(cookies=latest_cookie, account_id=str(account.goofish_id or account.id))
                latest_cookie = str(result.get("cookies_str") or latest_cookie)
                success_count += 1
            except Exception as exc:  # noqa: BLE001
                status = "failed"
                message = str(exc)[:1000]
                failed_count += 1
            if latest_cookie and latest_cookie != account.cookie:
                account.cookie = latest_cookie
                session.add(AccountCookie(account_id=account.id, cookie_value=latest_cookie, status="active"))
            session.add(FeatureRecord(
                owner_id=int(account.user_id), feature="close-notice-logs", external_id=uuid4().hex,
                status=status, payload={"batch_id": batch_id, "account_id": str(account.id), "status": status, "error_message": message}, note=message or "账号消息通知已关闭",
            ))
            await asyncio.sleep(1)
        await session.commit()
    return {"task_name": "close_notice", "status": "partial" if failed_count else "completed", "batch_id": batch_id, "total_accounts": len(accounts), "success_count": success_count, "failed_count": failed_count, "detail": f"关闭通知处理 {len(accounts)} 个账号，成功 {success_count} 个", "executed_at": _now().isoformat()}


async def execute_red_flower(account_id: int | None = None) -> dict[str, Any]:
    """处理开启自动求小红花的账号近十天未处理订单。"""
    batch_id = uuid4().hex
    cutoff = _now() - timedelta(days=10)
    total_orders = success_count = failed_count = 0
    excluded_statuses = {"cancelled", "processing", "refunding", "refunded"}
    async with async_session_maker() as session:
        account_query = select(Account).where(Account.status == "active", Account.cookie.isnot(None), Account.cookie != "")
        if account_id is not None:
            account_query = account_query.where(Account.id == account_id)
        accounts = list((await session.execute(account_query.order_by(Account.id.asc()))).scalars().all())
        for account in accounts:
            setting = (await session.execute(select(FeatureRecord).where(FeatureRecord.owner_id == account.user_id, FeatureRecord.feature == "account-settings", FeatureRecord.external_id == str(account.id)))).scalar_one_or_none()
            if not setting or not bool((setting.payload or {}).get("auto_red_flower")):
                continue
            orders = list((await session.execute(select(Order).where(Order.account_id == account.id, Order.is_red_flower.is_(False), Order.status.notin_(excluded_statuses), Order.placed_at.isnot(None), Order.placed_at >= cutoff).order_by(Order.placed_at.asc(), Order.id.asc()))).scalars().all())
            current_cookie = str(account.cookie or "")
            for order in orders:
                total_orders += 1
                status = "success"
                error_message = None
                try:
                    result = await request_red_flower(cookies=current_cookie, account_id=str(account.goofish_id or account.id), order_no=str(order.order_no))
                    current_cookie = str(result.get("cookies_str") or current_cookie)
                    order.is_red_flower = True
                    success_count += 1
                except Exception as exc:  # noqa: BLE001
                    status = "failed"
                    error_message = str(exc)[:1000]
                    failed_count += 1
                session.add(FeatureRecord(owner_id=int(account.user_id), feature="red-flower-logs", external_id=uuid4().hex, status=status, payload={"batch_id": batch_id, "account_id": str(account.id), "order_no": str(order.order_no), "status": status, "error_message": error_message}, note=error_message or "求小红花成功"))
                await asyncio.sleep(0.5)
            if current_cookie != account.cookie:
                account.cookie = current_cookie
                session.add(AccountCookie(account_id=account.id, cookie_value=current_cookie, status="active"))
        await session.commit()
    return {"task_name": "red_flower", "status": "partial" if failed_count else "completed", "batch_id": batch_id, "total_orders": total_orders, "success_count": success_count, "failed_count": failed_count, "detail": f"求小红花处理 {total_orders} 笔订单，成功 {success_count} 笔", "executed_at": _now().isoformat()}


async def execute_delivery_timeout() -> dict[str, Any]:
    cutoff = _now() - timedelta(minutes=5)
    async with async_session_maker() as session:
        records = list((await session.execute(select(CardDeliveryRecord).where(CardDeliveryRecord.status == "sending", CardDeliveryRecord.created_at < cutoff))).scalars().all())
        for record in records:
            record.status = "timeout"
            record.error_code = "delivery_timeout"
            record.error_message = "发货消息超过5分钟未确认"
            order = (await session.execute(select(Order).where(Order.id == record.order_id))).scalar_one_or_none()
            if order is not None and order.delivery_send_status == "sending":
                order.delivery_send_status = "timeout"
                order.delivery_send_fail_reason = record.error_message
        await session.commit()
    return {"task_name": "shipment_timeout", "status": "completed", "timeout_count": len(records), "detail": f"已标记 {len(records)} 笔超时发货尝试", "executed_at": _now().isoformat()}


async def _write_alert(session, external_id: str, owner_id: int, payload: dict[str, Any], message: str) -> None:
    exists = (await session.execute(select(FeatureRecord).where(FeatureRecord.feature == "timeout-alerts", FeatureRecord.external_id == external_id))).scalar_one_or_none()
    if exists is None:
        session.add(FeatureRecord(owner_id=owner_id, feature="timeout-alerts", external_id=external_id, status="detected", payload=payload, note=message))


async def execute_refund_timeout() -> dict[str, Any]:
    cutoff = _now() - timedelta(hours=48)
    async with async_session_maker() as session:
        cases = list((await session.execute(select(RefundCase).where(RefundCase.status == "open", RefundCase.created_at < cutoff))).scalars().all())
        for case in cases:
            await _write_alert(session, f"refund-{case.id}", int(case.owner_id), {"type": "refund", "case_id": case.id, "order_id": case.order_id, "detected_at": _now().isoformat()}, "退款申请超过48小时仍未处理")
        await session.commit()
    return {"task_name": "refund_timeout", "status": "completed", "timeout_count": len(cases), "detail": f"发现 {len(cases)} 笔超时退款待处理", "executed_at": _now().isoformat()}


async def execute_shipment_pending_timeout() -> dict[str, Any]:
    cutoff = _now() - timedelta(hours=24)
    async with async_session_maker() as session:
        orders = list((await session.execute(select(Order, Account).join(Account, Account.id == Order.account_id).where(Order.status.in_({"paid", "pending_ship"}), Order.created_at < cutoff))).all())
        for order, account in orders:
            await _write_alert(session, f"shipment-{order.id}", int(account.user_id), {"type": "shipment", "order_id": order.id, "order_no": order.order_no, "account_id": account.id, "detected_at": _now().isoformat()}, "订单超过24小时仍未完成发货")
        await session.commit()
    return {"task_name": "shipment_timeout", "status": "completed", "timeout_count": len(orders), "detail": f"发现 {len(orders)} 笔超时待发货订单", "executed_at": _now().isoformat()}


async def execute_crawl_goofish() -> dict[str, Any]:
    async with async_session_maker() as session:
        statement = select(GoofishCrawlJob).where(GoofishCrawlJob.enabled == 1)
        jobs = list((await session.execute(statement.order_by(GoofishCrawlJob.id.asc()))).scalars().all())
        total_fetched = total_saved = failed = 0
        details = []
        for job in jobs:
            account = (await session.execute(select(Account).where(Account.id == job.account_id))).scalar_one_or_none()
            if account is None or not (account.cookie or "").strip():
                job.last_error = "采集账号没有有效 Cookie，请先扫码登录"; failed += 1
                details.append({"job_id": job.id, "status": "failed", "message": job.last_error})
                continue
            fetched = 0; saved = 0
            try:
                client = GoofishClient(account.cookie, account.proxy)
                for page in range(job.start_page, job.start_page + max(1, job.pages)):
                    response = await client.search(job.keyword, page, job.page_size)
                    rows = response.get("items") or []
                    fetched += len(rows)
                    for row in rows:
                        external_id = str(row.get("item_id") or "").strip()
                        if not external_id:
                            continue
                        result = (await session.execute(select(GoofishCrawlResult).where(GoofishCrawlResult.job_id == job.id, GoofishCrawlResult.external_id == external_id))).scalar_one_or_none()
                        if result is None:
                            result = GoofishCrawlResult(job_id=job.id, external_id=external_id, title=str(row.get("title") or "未命名商品")); session.add(result)
                        result.title = str(row.get("title") or "未命名商品"); result.price = row.get("price"); result.seller = row.get("seller"); result.area = row.get("area"); result.url = row.get("url"); result.payload = row.get("raw") if isinstance(row.get("raw"), dict) else row; result.fetched_at = _now(); saved += 1
                job.last_run_at = _now(); job.last_error = None
                total_fetched += fetched; total_saved += saved
                details.append({"job_id": job.id, "status": "success", "fetched": fetched, "saved": saved})
            except Exception as exc:
                job.last_run_at = _now(); job.last_error = str(exc)[:2000]; failed += 1
                details.append({"job_id": job.id, "status": "failed", "message": job.last_error})
            await session.commit()
    return {"task_name": "crawl_goofish", "status": "partial" if failed else "completed", "fetched_count": total_fetched, "saved_count": total_saved, "failed_count": failed, "details": details, "executed_at": _now().isoformat()}


async def execute_listing_monitors(monitor_type: str | None = None) -> dict[str, Any]:
    """执行新版监控任务的真实闲鱼搜索并更新快照。"""
    async with async_session_maker() as session:
        tasks = list((await session.execute(select(FeatureRecord).where(FeatureRecord.feature == "listing-monitor-tasks"))).scalars().all())
        fetched = inserted = updated = failed = 0
        for task_row in tasks:
            task = task_row.payload or {}
            if not bool(task.get("is_enabled", True)) or (monitor_type and str(task.get("monitor_type") or "listing") != monitor_type):
                continue
            owner_id = int(task_row.owner_id)
            account_ids = [int(value) for value in task.get("account_ids") or [] if str(value).isdigit()]
            statement = select(Account).where(Account.user_id == owner_id, Account.status == "active", Account.cookie.isnot(None), Account.cookie != "")
            if account_ids:
                statement = statement.where(Account.id.in_(account_ids))
            accounts = list((await session.execute(statement.order_by(Account.id.asc()))).scalars().all())
            log = {"monitor_task_id": task_row.id, "monitor_type": task.get("monitor_type", "listing"), "keyword": task.get("keyword", ""), "trigger_type": "scheduled", "used_account_ids": [], "pages": int(task.get("collect_pages") or 1), "fetched_count": 0, "inserted_count": 0, "updated_count": 0, "status": "success", "message": "采集完成"}
            try:
                if not accounts:
                    raise RuntimeError("没有可用的已登录采集账号")
                for account in accounts:
                    log["used_account_ids"].append(str(account.id))
                    client = GoofishClient(account.cookie or "", account.proxy)
                    for page in range(1, max(1, min(int(task.get("collect_pages") or 1), 20)) + 1):
                        result = await client.search(str(task.get("keyword") or ""), page, 20)
                        rows = result.get("items") or []
                        fetched += len(rows); log["fetched_count"] += len(rows)
                        for value in rows:
                            item_id = str(value.get("item_id") or "").strip()
                            if not item_id:
                                continue
                            candidates = list((await session.execute(select(FeatureRecord).where(FeatureRecord.feature == "listing-monitor-items", FeatureRecord.owner_id == owner_id))).scalars().all())
                            existing = next((candidate for candidate in candidates if str((candidate.payload or {}).get("monitor_task_id")) == str(task_row.id) and str((candidate.payload or {}).get("item_id")) == item_id), None)
                            old_payload = dict(existing.payload or {}) if existing else {}
                            new_payload = {"monitor_task_id": task_row.id, "monitor_task_keyword": task.get("keyword"), "item_id": item_id, "title": value.get("title"), "price": value.get("price"), "area": value.get("area"), "pic_url": value.get("pic_url"), "seller_id": value.get("seller"), "target_url": value.get("url"), "is_dm_sent": bool(old_payload.get("is_dm_sent", False)), "is_ordered": bool(old_payload.get("is_ordered", False)), "last_seen_at": _now().isoformat(), "raw_json": value.get("raw") if isinstance(value.get("raw"), dict) else value}
                            if str(task.get("monitor_type") or "listing") == "price_drop" and old_payload.get("price") is not None and value.get("price") is not None:
                                try:
                                    if float(value["price"]) < float(old_payload["price"]):
                                        new_payload["change_note"] = f"价格从 {old_payload['price']} 降至 {value['price']}"
                                except (TypeError, ValueError):
                                    pass
                            if existing is None:
                                session.add(FeatureRecord(owner_id=owner_id, feature="listing-monitor-items", external_id=item_id, status="active", payload=new_payload, note="监控采集到商品")); inserted += 1; log["inserted_count"] += 1
                            else:
                                existing.payload = {**old_payload, **new_payload}; updated += 1; log["updated_count"] += 1
                task_row.payload = {**task, "last_run_at": _now().isoformat()}
            except Exception as exc:
                failed += 1; log["status"] = "failed"; log["message"] = str(exc)[:1000]
            session.add(FeatureRecord(owner_id=owner_id, feature="listing-monitor-logs", external_id=uuid4().hex, status=log["status"], payload=log, note=log["message"])); await session.commit()
        status = "partial" if failed else "completed"
    return {"task_name": "monitor_listings" if monitor_type != "price_drop" else "monitor_price", "status": status, "fetched_count": fetched, "inserted_count": inserted, "updated_count": updated, "failed_count": failed, "detail": f"监控采集抓取 {fetched} 条，新增 {inserted} 条", "executed_at": _now().isoformat()}


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return "X'" + bytes(value).hex() + "'"
    if isinstance(value, datetime):
        value = value.isoformat(sep=" ")
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


async def execute_database_backup() -> dict[str, Any]:
    started = time.perf_counter()
    backup_root = Path(settings.backup_dir).resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    filename = f"backup_{_now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}.sql.gz"
    path = backup_root / filename
    table_count = total_rows = 0
    owner_id = 1
    try:
        async with async_session_maker() as session:
            tables_result = await session.execute(text("SHOW TABLES"))
            table_names = [str(row[0]) for row in tables_result.fetchall() if row and row[0]]
            output: list[str] = ["-- xianyu-rewrite database backup\n", f"-- created_at: {_now().isoformat()}\n", "SET FOREIGN_KEY_CHECKS=0;\n"]
            for table in table_names:
                if "_log" in table.lower() or "_logs" in table.lower():
                    include_rows = False
                else:
                    include_rows = True
                create_result = await session.execute(text(f"SHOW CREATE TABLE `{table.replace('`', '``')}`"))
                create_row = create_result.first()
                if create_row is None:
                    continue
                create_sql = str(create_row[1]).rstrip(";")
                output.append(create_sql + ";\n")
                if not include_rows:
                    continue
                rows_result = await session.execute(text(f"SELECT * FROM `{table.replace('`', '``')}`"))
                rows = rows_result.fetchall()
                columns = list(rows_result.keys())
                if rows:
                    quoted = ",".join(f"`{str(column).replace('`', '``')}`" for column in columns)
                    for row in rows:
                        output.append(f"INSERT INTO `{table.replace('`', '``')}` ({quoted}) VALUES ({','.join(_sql_literal(value) for value in row)});\n")
                    total_rows += len(rows)
                table_count += 1
            output.append("SET FOREIGN_KEY_CHECKS=1;\n")
            with gzip.open(path, "wt", encoding="utf-8") as stream:
                stream.writelines(output)
            for old in backup_root.glob("backup_*.sql.gz"):
                if old == path:
                    continue
                try:
                    if old.stat().st_mtime < (time.time() - 10 * 86400):
                        old.unlink()
                except OSError:
                    pass
            record = FeatureRecord(owner_id=owner_id, feature="db-backup-logs", external_id=uuid4().hex, status="success", payload={"file_name": filename, "file_size": path.stat().st_size, "table_count": table_count, "total_rows": total_rows, "duration_ms": int((time.perf_counter() - started) * 1000), "error_message": None, "downloadable": True}, note="数据库备份成功")
            session.add(record)
            await session.commit()
    except Exception as exc:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        async with async_session_maker() as session:
            session.add(FeatureRecord(owner_id=owner_id, feature="db-backup-logs", external_id=uuid4().hex, status="failed", payload={"file_name": filename, "file_size": None, "table_count": table_count, "total_rows": total_rows, "duration_ms": int((time.perf_counter() - started) * 1000), "error_message": str(exc)[:2000], "downloadable": False}, note="数据库备份失败"))
            await session.commit()
        return {"task_name": "backup_database", "status": "failed", "detail": str(exc)[:500], "executed_at": _now().isoformat()}
    return {"task_name": "backup_database", "status": "completed", "file_name": filename, "table_count": table_count, "total_rows": total_rows, "detail": "数据库备份完成", "executed_at": _now().isoformat()}


async def execute_cleanup_uploads() -> dict[str, Any]:
    root = Path(settings.static_dir).resolve()
    removed = 0
    cutoff = time.time() - 24 * 3600
    for relative in (Path("uploads/chat"), Path("uploads/products"), Path("uploads/item-replies")):
        directory = root / relative
        if not directory.is_dir():
            continue
        for item in directory.iterdir():
            if item.is_file() and item.stat().st_mtime < cutoff:
                try:
                    item.unlink(); removed += 1
                except OSError:
                    pass
    return {"task_name": "cleanup_uploads", "status": "completed", "removed_count": removed, "detail": f"已清理 {removed} 个过期临时文件", "executed_at": _now().isoformat()}


async def execute_publish_retry() -> dict[str, Any]:
    """重试具备完整商品快照的失败发布任务。

    单品发布日志只保存平台结果，批量发布日志还保存素材快照引用；只有
    后者具备安全重试所需的图片、分类和规格数据。没有快照的旧日志会被
    明确统计为不可重试，不会伪造一次发布成功。
    """
    attempted = success = failed = skipped = 0
    async with async_session_maker() as session:
        rows = list((await session.execute(select(FeatureRecord).where(FeatureRecord.feature == "product-publish-jobs", FeatureRecord.status == "failed").order_by(FeatureRecord.id.asc()).limit(100))).scalars().all())
        materials = {row.id: row for row in (await session.execute(select(FeatureRecord).where(FeatureRecord.feature == "product-materials"))).scalars().all()}
        for row in rows:
            payload = dict(row.payload or {})
            retry_count = int(payload.get("retry_count") or 0)
            if retry_count >= 3:
                skipped += 1
                continue
            account_id = int(payload.get("account_id") or 0) if str(payload.get("account_id") or "").isdigit() else 0
            account = (await session.execute(select(Account).where(Account.id == account_id))).scalar_one_or_none()
            material_id = int(payload.get("material_id") or 0) if str(payload.get("material_id") or "").isdigit() else 0
            material = materials.get(material_id)
            item_data = dict(material.payload or {}) if material else dict(payload)
            if material:
                item_data.update({"id": material.id, "material_id": material.id})
            if not account or not (account.cookie or "").strip() or not item_data.get("images"):
                payload["retry_count"] = retry_count + 1
                payload["retry_skipped_reason"] = "缺少有效账号或完整商品素材快照"
                row.payload = payload; row.note = payload["retry_skipped_reason"]; skipped += 1
                await session.commit()
                continue
            attempted += 1
            try:
                capability = await detect_publish_capability(cookie=account.cookie, platform_account_id=str(account.goofish_id or account.id), proxy=account.proxy)
                if not capability.get("success"):
                    result = {"success": False, "message": str(capability.get("message") or "账号发布能力检测失败"), "cookies_str": capability.get("cookies_str") or account.cookie}
                else:
                    result = await publish_item(item_data={**item_data, "account_id": str(account.id)}, cookie=account.cookie, platform_account_id=str(account.goofish_id or account.id), proxy=account.proxy, is_fish_shop=bool(capability.get("is_fish_shop")))
            except GoofishPublishError as exc:
                result = {"success": False, "message": str(exc), "cookies_str": account.cookie}
            except Exception as exc:  # noqa: BLE001
                result = {"success": False, "message": f"重试发布异常：{str(exc)[:500]}", "cookies_str": account.cookie}
            latest_cookie = str(result.get("cookies_str") or account.cookie or "")
            if latest_cookie and latest_cookie != account.cookie:
                account.cookie = latest_cookie
                session.add(AccountCookie(account_id=account.id, cookie_value=latest_cookie, status="active"))
            payload.update({"retry_count": retry_count + 1, "item_id": result.get("item_id"), "item_url": result.get("item_url"), "error_message": None if result.get("success") else str(result.get("message") or "发布失败")[:2000]})
            row.payload = payload; row.status = "success" if result.get("success") else "failed"; row.note = str(result.get("message") or ("商品发布成功" if result.get("success") else "商品发布失败"))[:2000]
            if result.get("success"): success += 1
            else: failed += 1
            await session.commit()
    return {"task_name": "publish_retry", "status": "partial" if failed else "completed", "attempted_count": attempted, "success_count": success, "failed_count": failed, "skipped_count": skipped, "detail": f"发布重试 {attempted} 条，成功 {success} 条，跳过 {skipped} 条", "executed_at": _now().isoformat()}


async def execute_crawl_supply() -> dict[str, Any]:
    """执行已配置的闲鱼采集任务，供“货源采集”任务名复用。"""
    result = await execute_crawl_goofish()
    result["task_name"] = "crawl_supply"
    result["detail"] = f"货源采集处理完成：{result.get('detail') or '无已启用采集任务'}"
    return result


async def execute_distribution_sync() -> dict[str, Any]:
    """校验分销链路中的卡券和对接记录，自动收敛失效对接状态。"""
    checked = disabled = agent_count = 0
    async with async_session_maker() as session:
        cards = {row.id: row for row in (await session.execute(select(FeatureRecord).where(FeatureRecord.feature == "cards"))).scalars().all()}
        docks = list((await session.execute(select(FeatureRecord).where(FeatureRecord.feature == "distribution-dock-records"))).scalars().all())
        for row in docks:
            data = dict(row.payload or {}); card = cards.get(int(data.get("card_id") or 0)); checked += 1
            if card is None or card.status in {"disabled", "inactive", "deleted"} or (isinstance(card.payload, dict) and card.payload.get("enabled") is False):
                if row.status != "disabled" or data.get("disable_reason") != "关联卡券已停用":
                    data.update({"status": False, "disable_reason": "关联卡券已停用"}); row.payload = data; row.status = "disabled"; disabled += 1
        agent_count = int((await session.execute(select(func.count()).select_from(FeatureRecord).where(FeatureRecord.feature == "distribution-agent-orders"))).scalar_one())
        await session.commit()
    return {"task_name": "distribution_sync", "status": "completed", "checked_count": checked, "disabled_count": disabled, "agent_order_count": agent_count, "detail": f"已校验 {checked} 条对接记录，收敛 {disabled} 条失效记录", "executed_at": _now().isoformat()}


__all__ = [
    "execute_auto_rate", "execute_delivery_timeout", "execute_refund_timeout", "execute_shipment_pending_timeout",
    "execute_crawl_goofish", "execute_crawl_supply", "execute_publish_retry", "execute_distribution_sync",
    "execute_database_backup", "execute_cleanup_uploads",
]
