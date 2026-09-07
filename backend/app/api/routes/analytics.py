# -*- coding: utf-8 -*-
"""数据大盘：从真实业务表即时汇总指标。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.db.session import get_session
from common.models import (
    Account, AccountContent, ChatMessageRecord, Message, Order, Product,
    PublishJob, RiskLog,
)
from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok

router = APIRouter(prefix="/api/v1/data-analysis", tags=["数据分析"])


async def count_rows(session: AsyncSession, model: type, field: str | None = None, user_id: int = 1) -> int:
    statement = select(func.count()).select_from(model)
    if field and hasattr(model, field): statement = statement.where(getattr(model, field) == user_id)
    return int((await session.execute(statement)).scalar_one())


@router.get("")
@router.get("/")
async def summary(user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    uid = int(user.get("sub", 1))
    synced_products = int(
        (
            await session.execute(
                select(func.count())
                .select_from(AccountContent)
                .join(Account, Account.id == AccountContent.account_id)
                .where(
                    Account.user_id == uid,
                    AccountContent.content_type == "product",
                    AccountContent.status == "on_sale",
                )
            )
        ).scalar_one()
        or 0
    )
    data = {
        "accounts": await count_rows(session, Account, "user_id", uid),
        "products": synced_products + await count_rows(session, Product),
        "publish_jobs": await count_rows(session, PublishJob, "owner_id", uid),
        "orders": await count_rows(session, Order),
        "messages": await count_rows(session, Message),
        "risk_events": await count_rows(session, RiskLog),
    }
    return ok({"metrics": data, "period": "all_time"})


@router.get("/summary")
async def summary_alias(user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await summary(user, session)


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _date_window(date_type: str, date_range: str | None) -> tuple[date, date]:
    today = datetime.now().date()
    if date_type == "recent1d":
        return today, today + timedelta(days=1)
    if date_type == "recent7d":
        return today - timedelta(days=6), today + timedelta(days=1)
    if date_type == "recent30d":
        return today - timedelta(days=29), today + timedelta(days=1)
    if date_type == "customDate":
        values = [item.strip() for item in str(date_range or "").split("|")]
        if len(values) != 2:
            raise HTTPException(status_code=422, detail="自定义日期范围格式应为 YYYYMMDD|YYYYMMDD")
        try:
            start = datetime.strptime(values[0], "%Y%m%d").date()
            end = datetime.strptime(values[1], "%Y%m%d").date()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="自定义日期格式无效") from exc
        if start > end:
            raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
        if (end - start).days > 90:
            raise HTTPException(status_code=422, detail="自定义日期范围不能超过 91 天")
        return start, end + timedelta(days=1)
    raise HTTPException(status_code=422, detail="不支持的日期范围")


def _day(value: datetime | None, fallback: datetime) -> date:
    return (value or fallback).date()


def _metric(name: str, value: float | int, previous: float | int = 0) -> dict[str, Any]:
    numeric = float(value or 0)
    previous_numeric = float(previous or 0)
    ratio = ((numeric - previous_numeric) * 100 / previous_numeric) if previous_numeric else None
    is_integer = numeric.is_integer()
    display = str(int(numeric)) if is_integer else f"{numeric:.2f}"
    return {
        "name": name,
        "cycle": "当前周期",
        "data": int(numeric) if is_integer else numeric,
        "dataFormat": "number",
        "dataStr": display,
        "decimal": not is_integer,
        "lastData": int(previous_numeric) if previous_numeric.is_integer() else previous_numeric,
        "lastDataFormat": "number",
        "lastDataStr": str(int(previous_numeric)) if previous_numeric.is_integer() else f"{previous_numeric:.2f}",
        "ratio": round(ratio, 2) if ratio is not None else None,
        "ratioFormat": f"{ratio:.2f}%" if ratio is not None else "-",
    }


async def _owned_account(account_id: int, user: dict[str, Any], session: AsyncSession) -> Account:
    statement = select(Account).where(Account.id == account_id, Account.user_id == int(user.get("sub", 1)))
    account = (await session.execute(statement)).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在或无权访问")
    return account


@router.post("/seller-summary")
async def seller_summary(
    payload: dict[str, Any] = Body(default_factory=dict),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Return a stable seller-summary shape from synchronized local data.

    Exposure and visit metrics are not inferred from order counts.  Until a
    platform analytics provider is configured, those unavailable metrics stay
    zero and are marked as local snapshots rather than fabricated values.
    """
    try:
        account_id = int(payload.get("account_id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="account_id 必须是整数") from exc
    account = await _owned_account(account_id, user, session)
    start, end = _date_window(str(payload.get("date_type") or "recent1d"), payload.get("date_range"))
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.min.time())
    orders = list(
        (
            await session.execute(
                select(Order).where(
                    Order.account_id == account.id,
                    Order.created_at >= start_dt,
                    Order.created_at < end_dt,
                )
            )
        ).scalars().all()
    )
    chat_rows = list(
        (
            await session.execute(
                select(ChatMessageRecord).where(
                    ChatMessageRecord.account_id == account.id,
                    ChatMessageRecord.message_time >= int(start_dt.timestamp() * 1000),
                    ChatMessageRecord.message_time < int(end_dt.timestamp() * 1000),
                )
            )
        ).scalars().all()
    )
    product_count = int(
        (
            await session.execute(
                select(func.count()).select_from(AccountContent).where(
                    AccountContent.account_id == account.id,
                    AccountContent.content_type == "product",
                    AccountContent.status == "on_sale",
                )
            )
        ).scalar_one()
        or 0
    )
    paid_statuses = {"paid", "processing", "pending_ship", "shipped", "completed"}
    refund_statuses = {"refunding", "refunded"}
    paid_orders = [order for order in orders if order.status in paid_statuses]
    refund_orders = [order for order in orders if order.status in refund_statuses]
    buyer_ids = {str(order.buyer_id) for order in paid_orders if order.buyer_id}
    chat_buyers = {
        str(row.sender_id)
        for row in chat_rows
        if not row.is_self and row.sender_id
    }
    amount = sum(float(order.amount or 0) for order in paid_orders)
    refund_amount = sum(float(order.amount or 0) for order in refund_orders)
    graph: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        day_orders = [order for order in orders if _day(order.created_at, start_dt) == cursor]
        day_paid = [order for order in day_orders if order.status in paid_statuses]
        day_refund = [order for order in day_orders if order.status in refund_statuses]
        graph.append({
            "ds": cursor.strftime("%Y%m%d"),
            "payAmt": round(sum(float(order.amount or 0) for order in day_paid), 2),
            "payOrdCnt": len(day_paid),
            "payByrCnt": len({str(order.buyer_id) for order in day_paid if order.buyer_id}),
            "showPv": 0,
            "showUv": 0,
            "ipv": 0,
            "ipvUv": 0,
            "vstPv": 0,
            "vstUv": 0,
            "chatUv": len({str(row.sender_id) for row in chat_rows if not row.is_self and row.sender_id}),
            "aov": round(sum(float(order.amount or 0) for order in day_paid) / len(day_paid), 2) if day_paid else 0,
            "rfdAmt": round(sum(float(order.amount or 0) for order in day_refund), 2),
            "rfdOrdCnt": len(day_refund),
            "showItmCnt": 0,
            "ipvItmCnt": 0,
            "stItmCnt": len(day_paid),
            "uctr": 0,
            "onlCnt": product_count,
            "rptOrdCnt": 0,
            "rptByrCnt": 0,
            "rpr": 0,
            "fstByrPayAmt": 0,
            "rptByrPayAmt": 0,
            "showPvCmpPctl": 0,
            "payOrdCntCmpPctl": 0,
            "rep3minUvRate": 0,
        })
        cursor += timedelta(days=1)
    banner = [
        _metric("payAmt", amount),
        _metric("payOrdCnt", len(paid_orders)),
        _metric("payByrCnt", len(buyer_ids)),
        _metric("rfdAmt", refund_amount),
        _metric("rfdOrdCnt", len(refund_orders)),
        _metric("chatUv", len(chat_buyers)),
        _metric("onlCnt", product_count),
    ]
    return ok(
        {
            "code": "SUCCESS",
            "data": {"graphBannerBenchData": {"bannerDataList": banner, "graphDataList": graph}},
            "extendInfo": {"realDateRange": [start.strftime("%Y%m%d"), (end - timedelta(days=1)).strftime("%Y%m%d")]},
            "msg": "数据来自本地同步快照；未接入的平台曝光指标暂为 0",
        },
        "卖家数据查询成功",
    )


@router.post("/browse-summary")
async def browse_summary(
    payload: dict[str, Any] = Body(default_factory=dict),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Return locally available traffic dimensions without inventing ratios."""
    try:
        account_id = int(payload.get("account_id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="account_id 必须是整数") from exc
    account = await _owned_account(account_id, user, session)
    start, end = _date_window(str(payload.get("date_type") or "recent1d"), payload.get("date_range"))
    rows = list(
        (
            await session.execute(
                select(AccountContent).where(
                    AccountContent.account_id == account.id,
                    AccountContent.content_type == "product",
                    AccountContent.synced_at >= datetime.combine(start, datetime.min.time()),
                    AccountContent.synced_at < datetime.combine(end, datetime.min.time()),
                )
            )
        ).scalars().all()
    )
    chat_rows = list(
        (
            await session.execute(
                select(ChatMessageRecord).where(
                    ChatMessageRecord.account_id == account.id,
                    ChatMessageRecord.is_self.is_(False),
                    ChatMessageRecord.message_time >= int(
                        datetime.combine(start, time.min).timestamp() * 1000
                    ),
                    ChatMessageRecord.message_time < int(
                        datetime.combine(end, time.min).timestamp() * 1000
                    ),
                )
            )
        ).scalars().all()
    )

    def distribution(keys: tuple[str, ...]) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in rows:
            payload_data = row.payload if isinstance(row.payload, dict) else {}
            card_data = payload_data.get("cardData") if isinstance(payload_data.get("cardData"), dict) else {}
            nested_item_cat = card_data.get("itemCatDTO") if isinstance(card_data.get("itemCatDTO"), dict) else {}
            value = next(
                (
                    str(
                        payload_data.get(key)
                        or card_data.get(key)
                        or nested_item_cat.get(key)
                        or ""
                    ).strip()
                    for key in keys
                    if payload_data.get(key) or card_data.get(key) or nested_item_cat.get(key)
                ),
                "",
            )
            if value:
                counts[value] = counts.get(value, 0) + 1
        total = sum(counts.values())
        return [
            {
                "profileCode": value,
                "profileVal": value,
                "usrRatio": round(count * 100 / total, 2) if total else 0,
                "usrRatioFormat": f"{count * 100 / total:.2f}%" if total else "0.00%",
            }
            for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    def product_distribution() -> list[dict[str, Any]]:
        """按商品名称统计，避免把平台内部类目 ID 直接展示给客户。"""
        counts: dict[str, int] = {}
        for row in rows:
            payload_data = row.payload if isinstance(row.payload, dict) else {}
            card_data = payload_data.get("cardData") if isinstance(payload_data.get("cardData"), dict) else {}
            title = str(
                row.title
                or payload_data.get("title")
                or card_data.get("title")
                or ""
            ).strip()
            if title:
                counts[title] = counts.get(title, 0) + 1
        total = sum(counts.values())
        return [
            {
                "profileCode": title,
                "profileVal": title,
                "usrRatio": round(count * 100 / total, 2) if total else 0,
                "usrRatioFormat": f"{count * 100 / total:.2f}%" if total else "0.00%",
            }
            for title, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    active_hours: dict[str, int] = {}
    for row in chat_rows:
        try:
            hour = datetime.fromtimestamp(
                int(row.message_time) / 1000,
                tz=timezone(timedelta(hours=8)),
            ).hour
        except (TypeError, ValueError, OverflowError):
            continue
        label = f"{hour:02d}:00-{hour:02d}:59"
        active_hours[label] = active_hours.get(label, 0) + 1
    active_total = sum(active_hours.values())
    buyer_active = [
        {
            "profileCode": label,
            "profileVal": label,
            "usrRatio": round(count * 100 / active_total, 2) if active_total else 0,
            "usrRatioFormat": f"{count * 100 / active_total:.2f}%" if active_total else "0.00%",
        }
        for label, count in sorted(active_hours.items(), key=lambda item: (-item[1], item[0]))
    ]

    return ok(
        {
            "code": "SUCCESS",
            "data": {
                "sceneSourceList": [],
                "itemCateList": product_distribution(),
                "buyerActiveList": buyer_active,
                "buyerProvinceList": distribution(("province", "buyerProvince", "area")),
            },
            "msg": "流量分布来自本地商品快照；未同步的平台画像维度为空",
        },
        "流量分布查询成功",
    )
