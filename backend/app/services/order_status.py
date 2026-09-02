"""把聊天中的订单系统消息回写到本地订单状态。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.chat import ChatMessageRecord
from common.models.orders import Order
from common.services.order_status import merge_order_status, status_from_chat_text


def _message_value(message: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = message.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _evidence(messages: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, dict[str, Any]] = {}
    unbound: list[dict[str, Any]] = []
    for message in messages:
        status = status_from_chat_text(message.get("text"))
        if status == "unknown":
            continue
        order_no = _message_value(message, "orderId", "order_id", "orderNo", "order_no")
        item_id = _message_value(message, "itemId", "item_id")
        buyer_id = _message_value(message, "senderId", "sender_id")
        if not order_no:
            unbound.append({"status": status, "item_id": item_id, "buyer_id": buyer_id})
            continue
        bucket = grouped.setdefault(
            order_no,
            {"status": "unknown", "item_id": "", "buyer_id": ""},
        )
        bucket["status"] = merge_order_status(bucket["status"], status)
        bucket["item_id"] = bucket["item_id"] or item_id
        bucket["buyer_id"] = bucket["buyer_id"] or buyer_id

    for item in unbound:
        candidates = list(grouped.values())
        if item["item_id"]:
            candidates = [row for row in candidates if row["item_id"] == item["item_id"]]
        if item["buyer_id"]:
            buyer_candidates = [row for row in candidates if row["buyer_id"] == item["buyer_id"]]
            if buyer_candidates:
                candidates = buyer_candidates
        if len(candidates) == 1:
            candidates[0]["status"] = merge_order_status(candidates[0]["status"], item["status"])
    return grouped, unbound


async def apply_live_order_status(
    db: AsyncSession,
    account_id: int,
    message: dict[str, Any],
) -> bool:
    """处理一条实时聊天消息，成功定位订单时回写状态。"""
    status = status_from_chat_text(message.get("text"))
    if status == "unknown":
        return False
    order_no = _message_value(message, "orderId", "order_id", "orderNo", "order_no")
    statement = select(Order).where(Order.account_id == account_id)
    if order_no:
        statement = statement.where(Order.order_no == order_no)
    else:
        buyer_id = _message_value(message, "senderId", "sender_id")
        item_id = _message_value(message, "itemId", "item_id")
        if buyer_id:
            statement = statement.where(Order.buyer_id == buyer_id)
        if item_id:
            statement = statement.where(Order.item_external_id == item_id)
        statement = statement.order_by(Order.id.desc()).limit(2)
    rows = list((await db.execute(statement)).scalars().all())
    if len(rows) != 1:
        return False
    order = rows[0]
    merged = merge_order_status(order.status, status)
    cleared_delivery_error = False
    if merged in {"shipped", "completed"} and (
        order.delivery_fail_reason or order.delivery_send_fail_reason
    ):
        order.delivery_fail_reason = None
        order.delivery_send_fail_reason = None
        cleared_delivery_error = True
    if merged == order.status and not cleared_delivery_error:
        return False
    order.status = merged
    await db.commit()
    return True


async def reconcile_cached_chat_orders(db: AsyncSession, account_id: int) -> int:
    """用已落库的聊天记录修复本地订单状态，不触发任何外部动作。"""
    rows = list(
        (
            await db.execute(
                select(ChatMessageRecord)
                .where(ChatMessageRecord.account_id == account_id)
                .order_by(ChatMessageRecord.message_time.asc(), ChatMessageRecord.id.asc())
            )
        ).scalars().all()
    )
    messages = []
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        messages.append(
            {
                "text": row.text or "",
                "orderId": payload.get("orderId") or payload.get("order_id"),
                "itemId": payload.get("itemId") or payload.get("item_id"),
                "senderId": payload.get("senderId") or payload.get("sender_id"),
            }
        )
    grouped, _ = _evidence(messages)
    changed = 0
    for order_no, bucket in grouped.items():
        if bucket["status"] == "unknown":
            continue
        order = (
            await db.execute(
                select(Order).where(Order.account_id == account_id, Order.order_no == order_no)
            )
        ).scalar_one_or_none()
        if order is None:
            continue
        merged = merge_order_status(order.status, bucket["status"])
        cleared_delivery_error = False
        if merged in {"shipped", "completed"} and (
            order.delivery_fail_reason or order.delivery_send_fail_reason
        ):
            order.delivery_fail_reason = None
            order.delivery_send_fail_reason = None
            cleared_delivery_error = True
        if merged != order.status or cleared_delivery_error:
            order.status = merged
            changed += 1
    if changed:
        await db.commit()
    return changed


__all__ = ["apply_live_order_status", "reconcile_cached_chat_orders"]
