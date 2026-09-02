# -*- coding: utf-8 -*-
"""闲鱼自动评价与补评价的共用执行器。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models import Account, AccountCookie, FeatureRecord, Order
from common.services.xianyu_platform import XianyuPlatformError, rate_buyer


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _replace(value: str, order: Order, account: Account) -> str:
    payload = order.payload if isinstance(order.payload, dict) else {}
    values = {
        "order_id": str(order.order_no or ""),
        "buyer_id": str(order.buyer_id or ""),
        "buyer_name": str(order.buyer_nick or ""),
        "item_id": str(order.item_external_id or ""),
        "item_title": str(order.item_title or ""),
        "amount": str(order.amount or ""),
        "account_id": str(account.id),
    }
    for key, item in values.items():
        value = value.replace("{" + key + "}", item)
    return value


def _api_value(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("reply", "content", "text", "value", "message"):
            if payload.get(key) not in (None, ""):
                return str(payload[key])
        for key in ("data", "result"):
            value = _api_value(payload.get(key))
            if value:
                return value
    elif isinstance(payload, list) and payload:
        return _api_value(payload[0])
    elif payload not in (None, ""):
        return str(payload)
    return ""


async def _api_feedback(url: str, order: Order, account: Account) -> str:
    target = _replace(url.strip(), order, account)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(target, params={"order_id": order.order_no, "buyer_id": order.buyer_id or "", "item_id": order.item_external_id or ""})
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise XianyuPlatformError(f"评价 API 请求失败：{str(exc)[:300]}") from exc
    result = _api_value(body).strip()
    if not result:
        raise XianyuPlatformError("评价 API 未返回有效评价内容")
    return result[:500]


async def _load_settings(session: AsyncSession, owner_id: int, account_id: int) -> dict[str, Any]:
    row = (
        await session.execute(
            select(FeatureRecord).where(
                FeatureRecord.owner_id == owner_id,
                FeatureRecord.feature == "account-settings",
                FeatureRecord.external_id == str(account_id),
            )
        )
    ).scalar_one_or_none()
    payload = dict(row.payload or {}) if row else {}
    auto_rate = payload.get("auto_rate") if isinstance(payload.get("auto_rate"), dict) else {}
    return {"scheduled_rate": bool(payload.get("scheduled_rate")), "auto_rate": auto_rate}


async def run_rate_batch(
    session: AsyncSession,
    accounts: list[Account],
    *,
    scheduled_only: bool = False,
) -> dict[str, Any]:
    """执行一批订单评价，平台未确认成功时订单不会被标记为已评价。"""
    batch_id = f"rate-{uuid4().hex}"
    owner_id = int(accounts[0].user_id) if accounts else 1
    parent = FeatureRecord(
        owner_id=owner_id, feature="rate-batches", external_id=batch_id, status="running",
        payload={"batch_id": batch_id, "executed_at": _now().isoformat(), "total_orders": 0, "success_count": 0, "failed_count": 0},
        note="评价任务执行中",
    )
    session.add(parent)
    await session.commit()
    total = success_count = failed_count = 0
    details: list[dict[str, Any]] = []
    for account in accounts:
        config = await _load_settings(session, int(account.user_id), int(account.id))
        auto_config = config.get("auto_rate") if isinstance(config.get("auto_rate"), dict) else {}
        if scheduled_only and not (config.get("scheduled_rate") or auto_config.get("enabled")):
            continue
        statement = select(Order).where(
            Order.account_id == account.id,
            Order.status.in_({"shipped", "completed"}),
            Order.is_rated.is_(False),
        ).order_by(Order.placed_at.asc(), Order.created_at.asc(), Order.id.asc())
        orders = list((await session.execute(statement)).scalars().all())
        for order in orders:
            total += 1
            status = "failed"
            message = "评价失败"
            try:
                rate_type = str(auto_config.get("rate_type") or "text").lower()
                if rate_type == "api":
                    feedback = await _api_feedback(str(auto_config.get("api_url") or ""), order, account)
                else:
                    feedback = str(auto_config.get("text_content") or "不错的买家").strip()
                if not feedback:
                    raise XianyuPlatformError("评价内容不能为空")
                result = await rate_buyer(
                    cookies=account.cookie or "",
                    account_id=str(account.goofish_id or account.id),
                    order_no=order.order_no,
                    feedback=feedback,
                )
                refreshed = str(result.get("cookies_str") or "").strip()
                if refreshed and refreshed != (account.cookie or "").strip():
                    account.cookie = refreshed
                    session.add(AccountCookie(account_id=account.id, cookie_value=refreshed, status="active"))
                order.is_rated = True
                status = "success"
                message = str(result.get("message") or "评价成功")
                success_count += 1
            except Exception as exc:  # platform failures are recorded per order
                message = str(exc)[:1000]
                failed_count += 1
            detail = {"batch_id": batch_id, "account_id": str(account.id), "order_no": order.order_no, "status": status, "error_message": None if status == "success" else message}
            details.append(detail)
            session.add(FeatureRecord(owner_id=int(account.user_id), feature="rate-logs", external_id=uuid4().hex, status=status, payload=detail, note=message))
            parent.payload = {**(parent.payload or {}), "total_orders": total, "success_count": success_count, "failed_count": failed_count}
            await session.commit()
    parent.status = "success" if failed_count == 0 else "partial" if success_count else "failed"
    parent.note = "评价任务完成"
    parent.payload = {**(parent.payload or {}), "total_orders": total, "success_count": success_count, "failed_count": failed_count}
    await session.commit()
    return {
        "batch_id": batch_id, "total_orders": total, "success_count": success_count, "failed_count": failed_count,
        "success_accounts": len({item["account_id"] for item in details if item["status"] == "success"}),
        "total_accounts": len(accounts), "details": details,
    }


__all__ = ["run_rate_batch"]
