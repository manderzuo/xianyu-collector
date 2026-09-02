# -*- coding: utf-8 -*-
"""商品监控后置动作：卖家补全、下单账号私信和自动拍下。

这些动作直接消费 ``listing-monitor-items`` 的结构化记录，与前端详情页
使用同一份状态。所有平台调用都要求返回明确成功结果后才更新成功标记，
避免旧版出现“本地已成功、闲鱼实际没有执行”的假成功。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select

from common.config import settings
from common.db.session import async_session_maker
from common.models import Account, AccountCookie, FeatureRecord, Order
from common.services.goofish_detail import fetch_item_detail
from common.services.xianyu_order import XianyuOrderClient


ITEM_FEATURE = "listing-monitor-items"
TASK_FEATURE = "listing-monitor-tasks"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _ids(values: Any) -> list[int]:
    return list(dict.fromkeys(int(value) for value in (values or []) if str(value).strip().isdigit()))


def _task_for(tasks: dict[int, FeatureRecord], item: FeatureRecord) -> dict[str, Any]:
    payload = item.payload or {}
    raw_id = payload.get("monitor_task_id")
    task = tasks.get(int(raw_id)) if str(raw_id).isdigit() else None
    return dict(task.payload or {}) if task else {}


async def _accounts_for(
    session: Any,
    owner_id: int,
    configured_ids: Any,
    restricted_account_id: int | None = None,
) -> list[Account]:
    ids = _ids(configured_ids)
    if restricted_account_id is not None:
        ids = [restricted_account_id]
    statement = select(Account).where(
        Account.user_id == owner_id,
        Account.status == "active",
        Account.cookie.isnot(None),
        Account.cookie != "",
    ).order_by(Account.id.asc())
    if ids:
        statement = statement.where(Account.id.in_(ids))
    return list((await session.execute(statement)).scalars().all())


def _save_cookie(session: Any, account: Account, cookie_value: str) -> None:
    cookie_value = str(cookie_value or "").strip()
    if cookie_value and cookie_value != str(account.cookie or ""):
        account.cookie = cookie_value
        session.add(AccountCookie(account_id=account.id, cookie_value=cookie_value, status="active"))


async def execute_seller_fill(account_id: int | None = None) -> dict[str, Any]:
    """调用商品详情接口，补全商品卖家真实 ID、昵称和详情快照。"""
    cutoff = _now() - timedelta(days=2)
    attempted = filled = failed = waiting = 0
    async with async_session_maker() as session:
        task_rows = list((await session.execute(select(FeatureRecord).where(FeatureRecord.feature == TASK_FEATURE))).scalars().all())
        tasks = {row.id: row for row in task_rows}
        item_rows = list((await session.execute(select(FeatureRecord).where(FeatureRecord.feature == ITEM_FEATURE).order_by(FeatureRecord.id.asc()).limit(300))).scalars().all())
        for row in item_rows:
            payload = dict(row.payload or {})
            if payload.get("seller_user_id") or payload.get("seller_fill_permanent"):
                continue
            seen_at = _parse_datetime(payload.get("last_seen_at")) or row.created_at
            if seen_at and seen_at < cutoff:
                continue
            task = _task_for(tasks, row)
            accounts = await _accounts_for(session, int(row.owner_id), task.get("account_ids"), account_id)
            if not accounts:
                payload.update({"seller_fill_status": "waiting", "seller_fill_fail_reason": "没有可用的已登录采集账号"})
                row.payload = payload
                waiting += 1
                await session.commit()
                continue

            item_id = str(payload.get("item_id") or "").strip()
            last_error = "商品详情获取失败"
            item_invalid = False
            for account in accounts:
                attempted += 1
                result = await fetch_item_detail(
                    cookies=str(account.cookie or ""),
                    account_id=str(account.goofish_id or account.id),
                    item_id=item_id,
                    proxy=account.proxy,
                )
                _save_cookie(session, account, str(result.get("cookies_str") or account.cookie or ""))
                last_error = str(result.get("error") or last_error)[:500]
                if result.get("success") and result.get("seller_user_id"):
                    payload.update({
                        "seller_user_id": str(result["seller_user_id"])[:128],
                        "seller_nick": str(result.get("seller_nick") or payload.get("seller_nick") or "")[:255],
                        "seller_fill_status": "filled",
                        "seller_fill_fail_reason": None,
                        "has_detail": True,
                        "detail_json": result.get("detail") or {},
                        "seller_filled_at": _now().isoformat(),
                    })
                    row.payload = payload
                    filled += 1
                    await session.commit()
                    break
                if result.get("item_invalid"):
                    item_invalid = True
                    break
            else:
                payload.update({
                    "seller_fill_status": "failed",
                    "seller_fill_fail_reason": last_error,
                    "seller_fill_attempts": int(payload.get("seller_fill_attempts") or 0) + 1,
                })
                row.payload = payload
                failed += 1
                await session.commit()
                continue

            if item_invalid and not payload.get("seller_user_id"):
                payload.update({
                    "seller_fill_status": "failed",
                    "seller_fill_fail_reason": last_error,
                    "seller_fill_permanent": True,
                    "seller_fill_attempts": int(payload.get("seller_fill_attempts") or 0) + 1,
                })
                row.payload = payload
                failed += 1
                await session.commit()
    status = "partial" if failed else "completed"
    return {
        "task_name": "seller_fill",
        "status": status,
        "attempted_count": attempted,
        "filled_count": filled,
        "failed_count": failed,
        "waiting_count": waiting,
        "detail": f"卖家信息补全处理 {filled + failed + waiting} 条，成功 {filled} 条，失败 {failed} 条",
        "executed_at": _now().isoformat(),
    }


async def _websocket_call(client: httpx.AsyncClient, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(
        f"{settings.websocket_service_url.rstrip('/')}{path}",
        json=payload,
        headers={"X-Internal-Token": settings.jwt_secret},
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(f"消息服务返回非 JSON（HTTP {response.status_code}）") from exc
    if not response.is_success or not isinstance(body, dict) or not body.get("success"):
        detail = body.get("detail") or body.get("message") if isinstance(body, dict) else response.text
        raise RuntimeError(str(detail or "消息服务未确认成功")[:500])
    data = body.get("data")
    return data if isinstance(data, dict) else {}


async def execute_dm_send(account_id: int | None = None) -> dict[str, Any]:
    """按下单成功记录使用同一账号创建会话并发送私信。"""
    cutoff = _now() - timedelta(days=2)
    attempted = sent = failed = waiting = 0
    async with async_session_maker() as session:
        task_rows = list((await session.execute(select(FeatureRecord).where(FeatureRecord.feature == TASK_FEATURE))).scalars().all())
        tasks = {row.id: row for row in task_rows}
        rows = list((await session.execute(select(FeatureRecord).where(FeatureRecord.feature == ITEM_FEATURE).order_by(FeatureRecord.id.asc()).limit(300))).scalars().all())
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
            for row in rows:
                payload = dict(row.payload or {})
                ordered_at = _parse_datetime(payload.get("ordered_at")) or row.created_at
                if not payload.get("is_ordered") or not payload.get("order_id") or payload.get("is_dm_sent") or (ordered_at and ordered_at < cutoff):
                    continue
                if int(payload.get("dm_attempts") or 0) >= 3:
                    continue
                task = _task_for(tasks, row)
                content = str(payload.get("dm_content") or task.get("dm_content") or "").strip()
                seller_id = str(payload.get("seller_user_id") or "").strip()
                order_account = str(payload.get("order_account_id") or "").strip()
                if not content or not seller_id or not order_account.isdigit():
                    payload.update({"dm_status": "waiting", "dm_fail_reason": "缺少私信内容、卖家真实ID或下单账号"})
                    row.payload = payload
                    waiting += 1
                    await session.commit()
                    continue
                accounts = await _accounts_for(session, int(row.owner_id), [order_account], account_id)
                if not accounts:
                    payload.update({"dm_status": "waiting", "dm_fail_reason": "下单账号没有有效登录态"})
                    row.payload = payload
                    waiting += 1
                    await session.commit()
                    continue
                account = accounts[0]
                if account_id is not None and account.id != account_id:
                    continue
                attempted += 1
                messages = [part.strip() for part in content.split("######") if part.strip()]
                if not messages:
                    payload.update({"dm_status": "waiting", "dm_fail_reason": "私信内容为空"})
                    row.payload = payload
                    waiting += 1
                    await session.commit()
                    continue
                try:
                    chat = await _websocket_call(
                        client,
                        f"/internal/chat/{account.id}/create-chat",
                        {"to_user_id": seller_id, "item_id": str(payload.get("item_id") or "")},
                    )
                    cid = str(chat.get("cid") or "").strip()
                    if not cid:
                        raise RuntimeError("创建会话未返回有效会话ID")
                    for message in messages:
                        await _websocket_call(
                            client,
                            f"/internal/chat/{account.id}/send-text",
                            {"cid": cid, "to_user_id": seller_id, "text": message},
                        )
                        if len(messages) > 1:
                            await asyncio.sleep(0.5)
                    payload.update({
                        "is_dm_sent": True,
                        "dm_status": "success",
                        "dm_account_id": str(account.id),
                        "dm_chat_id": cid,
                        "dm_fail_reason": None,
                        "dm_attempts": int(payload.get("dm_attempts") or 0) + 1,
                        "dm_sent_at": _now().isoformat(),
                    })
                    row.payload = payload
                    sent += 1
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    payload.update({
                        "dm_status": "waiting" if "未加载" in str(exc) or "连接" in str(exc) else "failed",
                        "dm_fail_reason": str(exc)[:500],
                        "dm_attempts": int(payload.get("dm_attempts") or 0) + 1,
                    })
                    row.payload = payload
                    failed += 1
                await session.commit()
    status = "partial" if failed else "completed"
    return {
        "task_name": "dm_send",
        "status": status,
        "attempted_count": attempted,
        "sent_count": sent,
        "failed_count": failed,
        "waiting_count": waiting,
        "detail": f"采集商品私信处理 {attempted} 条，成功 {sent} 条，失败 {failed} 条",
        "executed_at": _now().isoformat(),
    }


async def execute_auto_order(account_id: int | None = None) -> dict[str, Any]:
    """对监控商品执行 render/create 拍下，不执行支付。"""
    cutoff = _now() - timedelta(days=2)
    attempted = ordered = failed = waiting = 0
    async with async_session_maker() as session:
        task_rows = list((await session.execute(select(FeatureRecord).where(FeatureRecord.feature == TASK_FEATURE))).scalars().all())
        tasks = {row.id: row for row in task_rows}
        rows = list((await session.execute(select(FeatureRecord).where(FeatureRecord.feature == ITEM_FEATURE).order_by(FeatureRecord.id.asc()).limit(100))).scalars().all())
        for row in rows:
            payload = dict(row.payload or {})
            seen_at = _parse_datetime(payload.get("last_seen_at")) or row.created_at
            if payload.get("is_ordered") or (seen_at and seen_at < cutoff) or int(payload.get("order_attempts") or 0) >= 3:
                continue
            task = _task_for(tasks, row)
            if not task or task.get("is_enabled", True) is False:
                continue
            configured = task.get("order_account_ids") or task.get("account_ids")
            accounts = await _accounts_for(session, int(row.owner_id), configured, account_id)
            if not accounts:
                payload.update({"order_status": "no_account", "order_fail_reason": "没有可用的已登录下单账号"})
                row.payload = payload
                waiting += 1
                await session.commit()
                continue
            item_id = str(payload.get("item_id") or "").strip()
            last_error = "下单失败"
            account_invalid = False
            usable_attempts = 0
            success_account: Account | None = None
            order_id = ""
            for account in accounts:
                attempted += 1
                client = XianyuOrderClient(
                    account_id=str(account.goofish_id or account.id),
                    cookies=str(account.cookie or ""),
                    proxy=account.proxy,
                )
                result = await client.place_order(item_id)
                _save_cookie(session, account, client.cookies_str)
                last_error = str(result.get("error") or last_error)[:500]
                if result.get("status") == "account_invalid":
                    account_invalid = True
                    continue
                usable_attempts += 1
                if result.get("status") == "success" and result.get("order_id"):
                    success_account = account
                    order_id = str(result["order_id"])
                    break
            if success_account is None:
                payload.update({
                    "order_status": "no_account" if account_invalid and usable_attempts == 0 else "failed",
                    "order_fail_reason": last_error,
                    "order_attempts": int(payload.get("order_attempts") or 0) + (0 if account_invalid and usable_attempts == 0 else 1),
                })
                row.payload = payload
                failed += 1
                await session.commit()
                continue
            existing_order = (await session.execute(select(Order).where(Order.order_no == order_id))).scalar_one_or_none()
            if existing_order is None:
                try:
                    amount = float(payload.get("price") or 0)
                except (TypeError, ValueError):
                    amount = 0.0
                session.add(Order(
                    account_id=success_account.id,
                    order_no=order_id,
                    item_external_id=item_id,
                    item_title=str(payload.get("title") or "")[:255],
                    amount=amount,
                    status="pending_payment",
                    placed_at=_now(),
                    payload={"source": "monitor_auto_order", "monitor_item_id": row.id},
                ))
            payload.update({
                "is_ordered": True,
                "order_status": "success",
                "order_fail_reason": None,
                "order_id": order_id,
                "order_account_id": str(success_account.id),
                "ordered_at": _now().isoformat(),
                "order_attempts": int(payload.get("order_attempts") or 0) + 1,
            })
            row.payload = payload
            ordered += 1
            await session.commit()
    status = "partial" if failed else "completed"
    return {
        "task_name": "auto_order",
        "status": status,
        "attempted_count": attempted,
        "ordered_count": ordered,
        "failed_count": failed,
        "waiting_count": waiting,
        "detail": f"自动拍下处理 {attempted} 次，成功 {ordered} 条，失败 {failed} 条（不执行付款）",
        "executed_at": _now().isoformat(),
    }


__all__ = ["execute_seller_fill", "execute_dm_send", "execute_auto_order"]
