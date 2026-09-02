"""订单状态事件的统一解析与合并规则。"""
from __future__ import annotations

from typing import Any


ORDER_STATUS_RANK = {
    "unknown": 0,
    "pending_payment": 10,
    "pending_ship": 20,
    "shipped": 30,
    "completed": 40,
    "refunding": 45,
    "refunded": 50,
    "cancelled": 50,
}


def status_from_chat_text(text: Any) -> str:
    """从闲鱼聊天系统消息中解析订单状态。"""
    value = str(text or "").strip()
    if not value:
        return "unknown"
    if any(marker in value for marker in ("退款成功", "钱款已原路退返", "已退款")):
        return "refunded"
    if any(marker in value for marker in ("退款申请", "申请退款", "退款中")):
        return "refunding"
    if any(marker in value for marker in ("交易关闭", "订单已关闭", "交易取消", "订单取消")):
        return "cancelled"
    if "交易成功" in value or "确认收货" in value:
        return "completed"
    if any(marker in value for marker in ("卖家已发货", "你已发货", "我已发货")):
        return "shipped"
    if "已发货" in value and "等待你发货" not in value:
        return "shipped"
    if any(marker in value for marker in ("待付款", "等待付款", "未付款")):
        return "pending_payment"
    if any(marker in value for marker in ("已付款", "等待你发货", "待发货")):
        return "pending_ship"
    return "unknown"


def merge_order_status(current: Any, incoming: Any) -> str:
    """合并状态事件，禁止旧消息把已完成订单降回待发货。"""
    current_value = str(current or "unknown")
    incoming_value = str(incoming or "unknown")
    if incoming_value not in ORDER_STATUS_RANK:
        return current_value
    if current_value not in ORDER_STATUS_RANK:
        current_value = "unknown"
    if current_value in {"refunded", "cancelled"} and incoming_value != current_value:
        return current_value
    if incoming_value in {"refunded", "cancelled"}:
        return incoming_value
    return incoming_value if ORDER_STATUS_RANK[incoming_value] > ORDER_STATUS_RANK[current_value] else current_value


__all__ = ["ORDER_STATUS_RANK", "status_from_chat_text", "merge_order_status"]
