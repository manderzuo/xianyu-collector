# -*- coding: utf-8 -*-
"""订单页面使用的兼容查询接口。

旧版页面把订单列表和详情当作独立业务接口使用；重写版的通用资源路由
返回的是统一资源包装。这里保留旧页面所需的分页、详情和本地删除语义，
数据仍来自当前订单表，不生成演示订单。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import delete, or_, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.services.card_delivery import auto_deliver_orders, deliver_order
from backend.app.services.order_status import reconcile_cached_chat_orders
from common.db.session import get_session
from common.models import Account, AccountCookie, CardDeliveryRecord, Order
from common.services.account_sync import sync_account_orders
from common.services.goofish_client import GoofishClient
from common.services.xianyu_platform import XianyuPlatformError, confirm_no_logistics

router = APIRouter(prefix="/api/v1/orders", tags=["订单兼容查询"])


def _uid(user: dict) -> int:
    """从当前认证主体中取出用户 ID，避免依赖账号路由的内部实现。"""
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _is_admin(user: dict) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _account_scope(statement, user: dict):
    if not _is_admin(user):
        statement = statement.where(Account.user_id == _uid(user))
    return statement


def serialize(order: Order) -> dict:
    return {
        "id": str(order.id),
        "order_id": order.order_no,
        "order_no": order.order_no,
        "account_id": order.account_id,
        "cookie_id": str(order.account_id),
        "item_id": str(order.item_external_id or order.product_id) if (order.item_external_id or order.product_id) is not None else "-",
        "item_title": order.item_title,
        "buyer_id": order.buyer_id or "-",
        "buyer_fish_nick": order.buyer_nick,
        "sku_info": f"{order.spec_name}: {order.spec_value}" if order.spec_name or order.spec_value else None,
        "quantity": int(order.quantity or 1),
        "amount": float(order.amount),
        "status": order.status,
        "is_bargain": False,
        "is_rated": bool(order.is_rated),
        "is_red_flower": bool(order.is_red_flower),
        "is_unregistered": False,
        "is_agent_order": False,
        "delivery_method": order.delivery_method,
        "delivery_content": order.delivery_content,
        "delivery_fail_reason": order.delivery_fail_reason,
        "delivery_send_status": order.delivery_send_status,
        "delivery_send_fail_reason": order.delivery_send_fail_reason,
        "card_only_delivered": bool(order.card_only_delivered),
        "source": "local",
        "placed_at": (order.placed_at or order.created_at).isoformat() if (order.placed_at or order.created_at) else None,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
    }


@router.get("")
@router.get("/")
async def list_orders(
    cookie_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    # 订单列表权限受闲鱼账号类型影响，部分账号无法访问 merchant.sold.get。
    # 先用已落库的聊天系统消息收敛状态，避免平台拉单失败时本地订单长期
    # 停留在待发货；此操作只读本地缓存，不会触发发货或其他外部动作。
    account_scope = _account_scope(select(Account.id), user)
    if cookie_id and cookie_id.isdigit():
        account_scope = account_scope.where(Account.id == int(cookie_id))
    for account_id in (await db.execute(account_scope)).scalars().all():
        await reconcile_cached_chat_orders(db, int(account_id))
    statement = _account_scope(select(Order).join(Account, Account.id == Order.account_id), user)
    if cookie_id and cookie_id.isdigit():
        statement = statement.where(Order.account_id == int(cookie_id))
    if status:
        status_value = {"pending_payment": "pending", "processing": "paid"}.get(status, status)
        statement = statement.where(Order.status == status_value)
    if search:
        term = f"%{search.strip()}%"
        statement = statement.where(or_(Order.order_no.like(term), Order.buyer_id.like(term)))
    total = int((await db.execute(select(func.count()).select_from(statement.subquery()))).scalar_one() or 0)
    rows = (await db.execute(statement.order_by(Order.id.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return ok({"items": [serialize(row) for row in rows], "total": total, "page": page, "page_size": page_size}, "订单查询成功")


@router.get("/{order_no}/delivery")
async def get_order_delivery(order_no: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    """查看订单的卡券发货尝试，供订单详情和故障重试使用。"""
    order = await _find_order(order_no, user, db)
    records = (
        await db.execute(
            select(CardDeliveryRecord)
            .where(CardDeliveryRecord.order_id == order.id)
            .order_by(CardDeliveryRecord.id.desc())
            .limit(20)
        )
    ).scalars().all()
    return ok({"order": serialize(order), "records": [
        {
            "id": record.id, "order_no": record.order_no, "card_id": record.card_id,
            "card_name": record.card_name, "status": record.status,
            "delivery_content": record.delivery_content, "message_ids": record.message_ids or [],
            "error_code": record.error_code, "error_message": record.error_message,
            "attempt_count": record.attempt_count, "sent_at": record.sent_at.isoformat() if record.sent_at else None,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }
        for record in records
    ]}, "发货记录查询成功")


@router.get("/{order_no}")
async def get_order(order_no: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    statement = _account_scope(select(Order).join(Account, Account.id == Order.account_id), user)
    if order_no.isdigit():
        statement = statement.where(or_(Order.id == int(order_no), Order.order_no == order_no))
    else:
        statement = statement.where(Order.order_no == order_no)
    order = (await db.execute(statement)).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    return ok(serialize(order), "订单详情查询成功")


@router.delete("/local/{order_id}")
async def delete_order(order_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    order = (
        await db.execute(
            _account_scope(select(Order).join(Account, Account.id == Order.account_id).where(Order.id == order_id), user)
        )
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    await db.delete(order)
    await db.commit()
    return ok({"id": order_id, "deleted": True}, "订单已删除")


async def _find_order(order_no: str, user: dict, db: AsyncSession) -> Order:
    statement = _account_scope(select(Order).join(Account, Account.id == Order.account_id), user)
    if order_no.isdigit():
        statement = statement.where(or_(Order.id == int(order_no), Order.order_no == order_no))
    else:
        statement = statement.where(Order.order_no == order_no)
    item = (await db.execute(statement)).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    return item


@router.put("/{order_id}/status")
async def update_order_status(order_id: int, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    order = await _find_order(str(order_id), user, db)
    status_value = str((payload or {}).get("status") or "").strip()
    allowed = {"pending", "paid", "pending_ship", "shipped", "completed", "refunding", "refunded", "cancelled", "unknown"}
    if status_value not in allowed:
        raise HTTPException(status_code=422, detail="订单状态无效")
    order.status = status_value
    await db.commit()
    return ok(serialize(order), "订单状态已更新")


@router.post("/batch-delete")
async def batch_delete_orders(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    ids = [int(value) for value in ((payload or {}).get("ids") or []) if str(value).isdigit()]
    deleted = 0
    if ids:
        owned = (
            await db.execute(
                _account_scope(
                    select(Order.id).join(Account, Account.id == Order.account_id).where(Order.id.in_(ids)),
                    user,
                )
            )
        ).scalars().all()
        if owned:
            result = await db.execute(delete(Order).where(Order.id.in_(owned)))
            deleted = int(result.rowcount or 0)
        await db.commit()
    return ok({"deleted": deleted, "ids": ids}, "订单已批量删除")


@router.post("/fetch-xianyu")
async def fetch_xianyu_orders(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    data = payload or {}
    cookie_id = data.get("cookie_id")
    statement = select(Account)
    if cookie_id not in (None, "") and not str(cookie_id).strip().isdigit():
        raise HTTPException(status_code=422, detail="同步账号无效，请重新选择账号")
    if cookie_id not in (None, ""):
        statement = statement.where(Account.id == int(cookie_id))
    if not _is_admin(user):
        statement = statement.where(Account.user_id == _uid(user))
    accounts = (await db.execute(statement.order_by(Account.id.asc()))).scalars().all()
    if not accounts:
        raise HTTPException(status_code=404, detail="没有找到可同步的账号")
    total_fetched = 0
    inserted = 0
    updated = 0
    errors: list[str] = []
    permission_limited_accounts: list[int] = []
    for account in accounts:
        account_id = int(account.id)
        account_name = str(account.account_name)
        try:
            if not account.cookie:
                raise ValueError("账号 Cookie 不可用，请先扫码登录")
            result = await sync_account_orders(db, account, GoofishClient(account.cookie, account.proxy), max_pages=100)
            delivery = await auto_deliver_orders(db, account)
            result["delivery"] = delivery
            total_fetched += int(result.get("fetched_count", 0) or 0)
            inserted += int(result.get("inserted_count", 0) or 0)
            updated += int(result.get("updated_count", 0) or 0)
        except Exception as exc:
            await db.rollback()
            error_text = str(exc)
            if "PERMISSION_EXCEPTION" in error_text or "无权限访问" in error_text:
                permission_limited_accounts.append(account_id)
                recovered = await reconcile_cached_chat_orders(db, account_id)
                errors.append(
                    f"{account_name}: 闲鱼订单列表接口无权限，已保留实时/聊天订单"
                    + (f"，并更新 {recovered} 条本地状态" if recovered else "")
                )
            else:
                errors.append(f"{account.account_name}: {error_text}")
    success = not errors
    return ok({"total_fetched": total_fetched, "new_inserted": inserted, "updated": updated, "failed": len(errors), "accounts_processed": len(accounts), "selected_account_id": int(cookie_id) if cookie_id not in (None, "") else None, "errors": errors, "permission_limited_accounts": permission_limited_accounts}, "订单同步完成" if success else "订单同步部分失败", code="ok" if success else "partial")


@router.post("/cancel")
async def cancel_order(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    order_no = str((payload or {}).get("order_no") or "").strip()
    if not order_no:
        raise HTTPException(status_code=422, detail="缺少订单号")
    order = await _find_order(order_no, user, db)
    order.status = "cancelled"
    await db.commit()
    return ok({"order_no": order.order_no, "status": order.status, "platform_sync": "pending"}, "订单已标记为取消，平台状态将在连接后同步")


@router.post("/manual-delivery")
async def manual_delivery(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    order_no = str((payload or {}).get("order_no") or "").strip()
    if not order_no:
        raise HTTPException(status_code=422, detail="缺少订单号")
    order = await _find_order(order_no, user, db)
    account = (await db.execute(select(Account).where(Account.id == order.account_id))).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="订单所属账号不存在")
    requested_card_id = (payload or {}).get("card_id")
    try:
        requested_card_id = int(requested_card_id) if requested_card_id is not None else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="卡券ID无效") from exc
    result = await deliver_order(db, order, account, source="manual", requested_card_id=requested_card_id)
    if result.get("status") == "sent":
        return ok(result, result.get("message") or "卡券发货成功")
    return {"success": False, "code": result.get("code") or "delivery_failed", "message": result.get("message") or "卡券发货失败", "data": result}


@router.post("/no-logistics-delivery")
async def no_logistics_delivery(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    data = payload or {}
    order_no = str(data.get("order_no") or data.get("order_id") or "").strip()
    if not order_no:
        raise HTTPException(status_code=422, detail="缺少订单号")
    order = await _find_order(order_no, user, db)
    account = (await db.execute(select(Account).where(Account.id == order.account_id))).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="订单所属账号不存在")
    if not (account.cookie or "").strip():
        return {"success": False, "code": "account_invalid", "message": "账号没有有效 Cookie，请先扫码登录", "data": {"status": "failed", "order_no": order.order_no}}
    if order.status not in {"pending", "paid", "processing", "pending_ship"}:
        return {"success": False, "code": "order_not_shippable", "message": "当前订单状态不可无物流发货", "data": serialize(order)}
    order_payload = order.payload if isinstance(order.payload, dict) else {}
    is_bargain = bool(data.get("is_bargain") or order_payload.get("is_bargain") or order_payload.get("isBargain"))
    try:
        result = await confirm_no_logistics(
            cookies=account.cookie or "",
            account_id=str(account.goofish_id or account.id),
            order_no=order.order_no,
            item_id=str(data.get("item_id") or order.item_external_id or order_payload.get("itemId") or ""),
            buyer_id=str(data.get("buyer_id") or order.buyer_id or ""),
            is_bargain=is_bargain,
        )
    except XianyuPlatformError as exc:
        return {"success": False, "code": "platform_delivery_failed", "message": str(exc), "data": {"status": "failed", "order_no": order.order_no}}
    refreshed_cookie = str(result.get("cookies_str") or "").strip()
    if refreshed_cookie and refreshed_cookie != (account.cookie or "").strip():
        account.cookie = refreshed_cookie
        db.add(AccountCookie(account_id=account.id, cookie_value=refreshed_cookie, status="active"))
    order.status = "shipped"
    order.delivery_method = "manual"
    order.delivery_content = "无物流发货"
    order.delivery_send_status = "success"
    order.delivery_fail_reason = None
    order.delivery_send_fail_reason = None
    order.delivered_at = datetime.utcnow()
    await db.commit()
    return ok({**serialize(order), "already_delivered": bool(result.get("already_delivered")), "platform_sync": "success"}, "无物流发货成功")
