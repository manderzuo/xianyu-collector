# -*- coding: utf-8 -*-
"""将已接入闲鱼账号的真实内容同步到本地工作区。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.account_contents import AccountContent, AccountSyncState
from common.models.accounts import Account
from common.models.messages import Message
from common.models.orders import Order
from common.services.goofish_client import GoofishClient, parse_cookie_string


ORDER_STATUS_MAP = {
    "待付款": "pending_payment",
    "待发货": "pending_ship",
    "已发货": "shipped",
    "交易成功": "completed",
    "交易关闭": "cancelled",
    "退款中": "refunding",
    "退款成功": "refunded",
    "已退款": "refunded",
    "退款关闭": "cancelled",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_platform_datetime(value: Any) -> datetime | None:
    """解析订单接口常见的秒/毫秒时间戳和 ISO 时间。"""
    if value in (None, ""):
        return None
    try:
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000
        return datetime.fromtimestamp(numeric, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def _account_platform_id(account: Account) -> str:
    """优先使用账号记录中的平台 ID，缺失时从登录 Cookie 的 unb 提取。"""
    if account.goofish_id and str(account.goofish_id).strip():
        return str(account.goofish_id).strip()
    cookie_value = parse_cookie_string(account.cookie or "").get("unb", "")
    match = re.search(r"\d{8,}", unquote(cookie_value))
    return match.group(0) if match else ""


def _image_list(value: Any) -> list[str]:
    """把闲鱼卡片中的图片字段压缩成可展示 URL 列表。"""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        values = value
    elif isinstance(value, dict):
        values = []
        for key in ("picUrl", "url", "image", "picList", "images"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                values.extend(candidate)
            elif candidate:
                values.append(candidate)
    else:
        return []
    urls: list[str] = []
    for item in values:
        if isinstance(item, str) and item:
            urls.append(item)
        elif isinstance(item, dict):
            url = item.get("url") or item.get("picUrl") or item.get("image")
            if url:
                urls.append(str(url))
    return list(dict.fromkeys(urls))


def _normalize_order(row: dict[str, Any]) -> dict[str, Any] | None:
    common = row.get("commonData") or {}
    buyer = row.get("buyerInfoVO") or {}
    price = row.get("priceVO") or {}
    item_info = row.get("itemInfoVO") or row.get("itemInfo") or row.get("itemVO") or {}
    sku_info = row.get("skuInfoVO") or row.get("skuInfo") or row.get("sku") or {}
    if not isinstance(common, dict):
        common = {}
    if not isinstance(buyer, dict):
        buyer = {}
    if not isinstance(price, dict):
        price = {}
    if not isinstance(item_info, dict):
        item_info = {}
    if not isinstance(sku_info, dict):
        sku_info = {}
    order_no = str(common.get("orderId") or "").strip()
    if not order_no:
        return None
    raw_status = str(common.get("orderStatus") or "")
    status = "refunding" if str(common.get("inRefund") or "").lower() == "true" else ORDER_STATUS_MAP.get(raw_status, "unknown")
    try:
        amount = float(Decimal(str(price.get("totalPrice") or "0")))
    except (ArithmeticError, TypeError, ValueError):
        amount = 0.0
    item_external_id = common.get("itemId") or common.get("itemID") or item_info.get("itemId") or item_info.get("id")
    item_title = common.get("itemTitle") or common.get("title") or item_info.get("title") or item_info.get("itemTitle")
    quantity_value = price.get("buyNum") or price.get("quantity") or common.get("buyNum") or 1
    try:
        quantity = max(1, int(quantity_value))
    except (TypeError, ValueError):
        quantity = 1
    spec_name = sku_info.get("specName") or sku_info.get("name") or common.get("specName")
    spec_value = sku_info.get("specValue") or sku_info.get("value") or common.get("specValue")
    placed_at = _parse_platform_datetime(common.get("createTime") or common.get("createdAt") or row.get("createTime"))
    rated_value = common.get("sellerRateStatus")
    is_rated = str(rated_value or "").strip().lower() in {"1", "true", "rated", "已评价", "success"}
    # 订单列表中的收货信息不进入快照，避免把手机号/地址写入通用 JSON 字段。
    safe_payload = {
            "commonData": {
            key: common.get(key)
            for key in ("orderId", "itemId", "itemID", "itemTitle", "orderStatus", "inRefund", "sellerRateStatus", "createTime", "buyNum", "specName", "specValue")
            if common.get(key) is not None
        },
        "buyerInfoVO": {
            key: buyer.get(key)
            for key in ("buyerId", "userNick")
            if buyer.get(key) is not None
        },
        "priceVO": {
            key: price.get(key)
            for key in ("totalPrice", "buyNum", "quantity")
            if price.get(key) is not None
        },
        "itemInfo": {
            key: item_info.get(key)
            for key in ("itemId", "id", "title", "itemTitle")
            if item_info.get(key) is not None
        },
        "skuInfo": {
            key: sku_info.get(key)
            for key in ("specName", "specValue", "name", "value")
            if sku_info.get(key) is not None
        },
    }
    return {
        "order_no": order_no[:64],
        "buyer_id": str(buyer.get("buyerId") or "")[:64] or None,
        "buyer_nick": str(buyer.get("userNick") or buyer.get("nick") or "")[:255] or None,
        "amount": amount,
        "status": status,
        "item_external_id": str(item_external_id or "")[:128] or None,
        "item_title": str(item_title or "")[:255] or None,
        "quantity": quantity,
        "spec_name": str(spec_name or "")[:128] or None,
        "spec_value": str(spec_value or "")[:255] or None,
        "is_rated": is_rated,
        "placed_at": placed_at,
        "payload": safe_payload,
    }


async def _get_or_create_state(session: AsyncSession, account_id: int) -> AccountSyncState:
    state = (
        await session.execute(
            select(AccountSyncState).where(AccountSyncState.account_id == account_id)
        )
    ).scalar_one_or_none()
    if state is None:
        state = AccountSyncState(account_id=account_id, status="never")
        session.add(state)
        await session.flush()
    return state


async def _acquire_sync_state(session: AsyncSession, account_id: int) -> AccountSyncState | None:
    """Acquire the database-backed account sync lease.

    The backend and scheduler are separate processes, so an in-memory lock is
    insufficient.  ``SELECT ... FOR UPDATE`` serializes existing state rows;
    the retry handles the first-sync race where both processes try to create
    the unique ``account_id`` row at the same time.  A lease older than thirty
    minutes is considered abandoned and can be recovered safely.
    """
    for attempt in range(2):
        try:
            state = (
                await session.execute(
                    select(AccountSyncState)
                    .where(AccountSyncState.account_id == account_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if state is None:
                state = AccountSyncState(account_id=account_id, status="never")
                session.add(state)
                await session.flush()

            now = _now()
            if (
                state.status == "syncing"
                and state.last_started_at is not None
                and now - state.last_started_at < timedelta(minutes=30)
            ):
                await session.rollback()
                return None

            state.status = "syncing"
            state.last_started_at = now
            state.last_finished_at = None
            state.last_error = None
            await session.commit()
            return state
        except IntegrityError:
            await session.rollback()
            if attempt == 1:
                raise
    return None


async def sync_account_products(
    session: AsyncSession,
    account: Account,
    *,
    page_size: int = 20,
    max_pages: int = 100,
    sync_products: bool = True,
    sync_orders: bool = True,
) -> dict[str, Any]:
    """按需同步账号商品/订单，并按平台 ID 幂等写入。

    默认同时同步商品和订单，保持原有手动同步行为；调度器可以按任务
    类型关闭其中一项，避免每五分钟拉订单时重复拉完整商品列表。
    """
    state = await _acquire_sync_state(session, account.id)
    if state is None:
        return {
            "status": "skipped",
            "reason": "sync_in_progress",
            "product_count": 0,
            "orders": {"status": "skipped", "reason": "sync_in_progress"},
            "messages": {"status": "available_via_chat"},
            "message": "该账号已有同步任务执行中，本次跳过",
        }

    if not account.cookie:
        state.status = "failed"
        state.last_error = "账号 Cookie 不可用，请先扫码登录"
        state.last_finished_at = _now()
        await session.commit()
        raise ValueError(state.last_error)

    platform_id = _account_platform_id(account)
    client = GoofishClient(account.cookie, account.proxy)
    seen_ids: set[str] = set()
    fetched_count = 0
    saved_count = 0
    changed_count = 0
    fetched_pages = 0

    try:
        if sync_products:
            for page in range(1, max_pages + 1):
                result = await client.list_account_items(
                    platform_id,
                    page=page,
                    page_size=page_size,
                )
                rows = result.get("items") or []
                if not rows:
                    break
                fetched_pages = page
                fetched_count += len(rows)
                ids = [str(row["external_id"]) for row in rows if row.get("external_id")]
                seen_ids.update(ids)
                existing_rows = (
                    await session.execute(
                        select(AccountContent).where(
                            AccountContent.account_id == account.id,
                            AccountContent.content_type == "product",
                            AccountContent.external_id.in_(ids),
                        )
                    )
                ).scalars().all()
                existing_map = {row.external_id: row for row in existing_rows}

                for row in rows:
                    external_id = str(row.get("external_id") or "").strip()
                    if not external_id:
                        continue
                    raw_payload = row.get("raw") if isinstance(row.get("raw"), dict) else row
                    values = {
                        "title": row.get("title") or "未命名商品",
                        "description": row.get("description"),
                        "price": row.get("price"),
                        "stock": int(row.get("stock") or 0),
                        "images": _image_list(row.get("images")),
                        "status": row.get("status") or "on_sale",
                        "payload": raw_payload,
                        "synced_at": _now(),
                    }
                    existing = existing_map.get(external_id)
                    if existing is None:
                        session.add(
                            AccountContent(
                                account_id=account.id,
                                content_type="product",
                                external_id=external_id,
                                **values,
                            )
                        )
                        saved_count += 1
                        changed_count += 1
                    else:
                        changed = any(
                            getattr(existing, key) != value
                            for key, value in values.items()
                            if key != "synced_at"
                        )
                        for key, value in values.items():
                            setattr(existing, key, value)
                        saved_count += 1
                        if changed:
                            changed_count += 1

                await session.commit()
                if len(rows) < page_size:
                    break

        # “在售”列表同步完整结束后，将本地已不再返回的旧商品标为下架，保留历史内容。
        if sync_products and fetched_pages < max_pages:
            old_rows = (
                await session.execute(
                    select(AccountContent).where(
                        AccountContent.account_id == account.id,
                        AccountContent.content_type == "product",
                        AccountContent.status == "on_sale",
                    )
                )
            ).scalars().all()
            for old in old_rows:
                if old.external_id not in seen_ids:
                    old.status = "off_sale"
            await session.commit()

        product_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(AccountContent).where(
                        AccountContent.account_id == account.id,
                        AccountContent.content_type == "product",
                    )
                )
            ).scalar_one()
            or 0
        )
        orders_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Order).where(Order.account_id == account.id)
                )
            ).scalar_one()
            or 0
        )
        messages_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Message).where(Message.account_id == account.id)
                )
            ).scalar_one()
            or 0
        )
        orders_result: dict[str, Any]
        if sync_orders:
            try:
                orders_result = await sync_account_orders(session, account, client, max_pages=max_pages)
            except Exception as exc:
                orders_result = {"status": "failed", "count": orders_count, "error": str(exc)[:1000]}
        else:
            orders_result = {"status": "skipped", "count": orders_count}
        orders_count = int(orders_result.get("count", orders_count) or 0)
        overall_status = "partial" if orders_result.get("status") == "failed" else "success"
        state.status = overall_status
        state.last_error = orders_result.get("error") if overall_status == "partial" else None
        state.products_count = product_count
        state.orders_count = orders_count
        state.messages_count = messages_count
        state.last_finished_at = _now()
        state.last_result = {
            "product_count": product_count,
            "total_count": fetched_count,
            "fetched_count": fetched_count,
            "saved_count": saved_count,
            "changed_count": changed_count,
            "pages": fetched_pages,
            "orders": orders_result,
            # 消息不再通过一次性同步接口拉取；由在线聊天连接实时写入消息表。
            "messages": {"status": "available_via_chat", "count": messages_count},
        }
        await session.commit()
        return {
            "status": overall_status,
            "product_count": product_count,
            "total_count": fetched_count,
            "fetched_count": fetched_count,
            "saved_count": saved_count,
            "changed_count": changed_count,
            "pages": fetched_pages,
            "orders": orders_result,
            "messages": {"status": "available_via_chat", "count": messages_count},
            "message": (
                ("商品和订单同步完成" if sync_products and sync_orders else "商品同步完成" if sync_products else "订单同步完成")
                + "；消息请在在线聊天中实时查看"
                if orders_result.get("status") != "failed"
                else "商品同步完成，但订单同步失败；消息请在在线聊天中实时查看"
            ),
        }
    except Exception as exc:
        await session.rollback()
        state = await _get_or_create_state(session, account.id)
        state.status = "failed"
        state.last_error = str(exc)[:2000]
        state.last_finished_at = _now()
        state.last_result = {"fetched_count": fetched_count, "pages": fetched_pages}
        await session.commit()
        raise


async def sync_account_orders(
    session: AsyncSession,
    account: Account,
    client: GoofishClient,
    *,
    max_pages: int = 100,
) -> dict[str, Any]:
    """拉取卖家订单列表并按订单号幂等更新本地订单表。"""
    fetched_count = 0
    inserted_count = 0
    updated_count = 0
    page_count = 0
    for page in range(1, max_pages + 1):
        result = await client.list_sold_orders(page=page, page_size=30)
        rows = result.get("items") or []
        if not rows:
            break
        page_count = page
        fetched_count += len(rows)
        for raw in rows:
            parsed = _normalize_order(raw)
            if parsed is None:
                continue
            existing = (
                await session.execute(
                    select(Order).where(Order.order_no == parsed["order_no"])
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    Order(
                        account_id=account.id,
                        order_no=parsed["order_no"],
                        buyer_id=parsed["buyer_id"],
                        buyer_nick=parsed["buyer_nick"],
                        product_id=None,
                        item_external_id=parsed["item_external_id"],
                        item_title=parsed["item_title"],
                        quantity=parsed["quantity"],
                        spec_name=parsed["spec_name"],
                        spec_value=parsed["spec_value"],
                        amount=parsed["amount"],
                        status=parsed["status"],
                        is_rated=parsed["is_rated"],
                        placed_at=parsed["placed_at"],
                        payload=parsed["payload"],
                    )
                )
                inserted_count += 1
            else:
                existing.account_id = account.id
                existing.buyer_id = parsed["buyer_id"] or existing.buyer_id
                existing.buyer_nick = parsed["buyer_nick"] or existing.buyer_nick
                existing.item_external_id = parsed["item_external_id"] or existing.item_external_id
                existing.item_title = parsed["item_title"] or existing.item_title
                existing.quantity = parsed["quantity"] or existing.quantity or 1
                existing.spec_name = parsed["spec_name"] or existing.spec_name
                existing.spec_value = parsed["spec_value"] or existing.spec_value
                existing.amount = parsed["amount"]
                existing.status = parsed["status"]
                existing.is_rated = parsed["is_rated"] or existing.is_rated
                existing.placed_at = parsed["placed_at"] or existing.placed_at
                existing.payload = parsed["payload"]
                updated_count += 1
        await session.commit()
        if len(rows) < 30 or not result.get("has_more"):
            break
    count = int(
        (
            await session.execute(
                select(func.count()).select_from(Order).where(Order.account_id == account.id)
            )
        ).scalar_one()
        or 0
    )
    return {
        "status": "success",
        "count": count,
        "fetched_count": fetched_count,
        "inserted_count": inserted_count,
        "updated_count": updated_count,
        "pages": page_count,
    }
