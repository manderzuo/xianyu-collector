# -*- coding: utf-8 -*-
"""账号内容同步和同步商品管理接口。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models.account_contents import AccountContent
from common.models.account_cookies import AccountCookie
from common.models.accounts import Account
from common.models.chat import ChatMessageRecord
from common.models.feature_records import FeatureRecord
from common.models.messages import Message
from common.models.orders import Order
from common.config import settings
from common.models.rules import DefaultReply, KeywordRule
from common.models.users import User
from common.services.account_sync import sync_account_products
from common.services.xianyu_platform import XianyuPlatformError, delete_items, offline_items
from backend.app.services.card_delivery import auto_deliver_orders

router = APIRouter(prefix="/api/v1", tags=["账号内容同步"])


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _owner_scope(user: dict[str, Any]) -> int | None:
    """管理员查看全量账号，普通用户只查看自己的账号。"""
    return None if _is_admin(user) else _uid(user)


def _account_scope(statement, user: dict[str, Any]):
    if not _is_admin(user):
        statement = statement.where(Account.user_id == _uid(user))
    return statement


def _value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _serialize(item: AccountContent, account_name: str | None = None) -> dict[str, Any]:
    data = {
        column.name: _value(getattr(item, column.name))
        for column in item.__table__.columns
        if column.name != "payload"
    }
    data["payload"] = item.payload
    if account_name is not None:
        data["account_name"] = account_name
    return data


async def _owned_account(account_id: int, user_id: int | None, db: AsyncSession) -> Account:
    statement = select(Account).where(Account.id == account_id)
    if user_id is not None:
        statement = statement.where(Account.user_id == user_id)
    account = (await db.execute(statement)).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return account


async def _account_by_identifier(identifier: Any, user: dict[str, Any], db: AsyncSession) -> Account:
    """兼容旧版 cookie_id：既支持本地账号主键，也支持闲鱼平台 goofish_id。"""
    value = str(identifier or "").strip()
    if not value:
        raise HTTPException(status_code=422, detail="请选择闲鱼账号")
    statement = select(Account).where((Account.goofish_id == value) | (Account.account_name == value))
    if value.isdigit():
        statement = select(Account).where(Account.id == int(value))
    if not _is_admin(user):
        statement = statement.where(Account.user_id == _uid(user))
    account = (await db.execute(statement)).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在或无权操作")
    return account


async def _owned_external_item(account_id: int, external_id: str, user_id: int | None, db: AsyncSession) -> AccountContent:
    await _owned_account(account_id, user_id, db)
    row = (
        await db.execute(
            select(AccountContent).where(
                AccountContent.account_id == account_id,
                AccountContent.content_type == "product",
                AccountContent.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    return row


def _payload(row: AccountContent) -> dict[str, Any]:
    return dict(row.payload or {})


async def _save_external_payload(row: AccountContent, values: dict[str, Any], db: AsyncSession) -> None:
    data = _payload(row)
    data.update(values)
    row.payload = data
    await db.commit()
    await db.refresh(row)


async def _persist_platform_cookie(account: Account, current_cookie: str, db: AsyncSession) -> None:
    """保存平台动作响应下发的新 Cookie，供后续签名和续期使用。"""
    current_cookie = str(current_cookie or "").strip()
    if not current_cookie or current_cookie == str(account.cookie or "").strip():
        return
    account.cookie = current_cookie
    db.add(AccountCookie(account_id=account.id, cookie_value=current_cookie, status="active"))


async def _count_keyword_rules(db: AsyncSession, user: dict[str, Any]) -> int:
    """统计重写版实际使用的关键词规则，兼容旧规则表回退。"""
    feature_statement = select(func.count()).select_from(FeatureRecord).where(
        FeatureRecord.feature == "keywords-with-item-id",
        FeatureRecord.status == "active",
    )
    if not _is_admin(user):
        feature_statement = feature_statement.where(FeatureRecord.owner_id == _uid(user))
    feature_count = int((await db.execute(feature_statement)).scalar_one() or 0)
    if feature_count:
        return feature_count

    legacy_statement = select(func.count()).select_from(KeywordRule)
    if not _is_admin(user):
        legacy_statement = legacy_statement.where(KeywordRule.owner_id == _uid(user))
    return int((await db.execute(legacy_statement)).scalar_one() or 0)


async def _online_account_ids() -> set[str]:
    """Read the account IDs whose connections are actually established."""
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            response = await client.get(
                f"{settings.websocket_service_url.rstrip('/')}/internal/accounts/connection-stats"
            )
        if not response.is_success:
            return set()
        payload = response.json()
        values = (payload.get("data") or {}).get("connected_account_ids") or []
        return {str(value) for value in values}
    except (httpx.HTTPError, ValueError, TypeError):
        return set()


async def _count_chat_replies(db: AsyncSession, user: dict[str, Any], day: date) -> int:
    """统计聊天流中的卖家发送消息，包含自动回复和人工回复。"""
    beijing = timezone(timedelta(hours=8))
    start = datetime.combine(day, datetime.min.time(), tzinfo=beijing)
    end = start + timedelta(days=1)
    statement = (
        select(func.count())
        .select_from(ChatMessageRecord)
        .join(Account, Account.id == ChatMessageRecord.account_id)
        .where(
            ChatMessageRecord.is_self.is_(True),
            ChatMessageRecord.message_time >= int(start.timestamp() * 1000),
            ChatMessageRecord.message_time < int(end.timestamp() * 1000),
        )
    )
    if not _is_admin(user):
        statement = statement.where(Account.user_id == _uid(user))
    return int((await db.execute(statement)).scalar_one() or 0)


@router.get("/cookies/stats")
async def account_stats(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    """为旧版仪表盘提供真实账号表的统计数据。"""
    user_id = _uid(user)
    account_filter = () if _is_admin(user) else (Account.user_id == user_id,)
    total_accounts = int((await db.execute(select(func.count()).select_from(Account).where(*account_filter))).scalar_one() or 0)
    active_accounts = int((await db.execute(select(func.count()).select_from(Account).where(Account.status == "active", *account_filter))).scalar_one() or 0)
    account_ids = {
        str(value)
        for value in (
            await db.execute(select(Account.id).where(*account_filter))
        ).scalars().all()
    }
    online_count = len(account_ids & await _online_account_ids())
    total_keywords = await _count_keyword_rules(db, user)
    total_orders = int(
        (
            await db.execute(
                _account_scope(
                    select(func.count())
                    .select_from(Order)
                    .join(Account, Account.id == Order.account_id),
                    user,
                )
            )
        ).scalar_one()
        or 0
    )
    today = datetime.now(timezone(timedelta(hours=8))).date()
    yesterday = today - timedelta(days=1)
    today_replies = await _count_chat_replies(db, user, today)
    yesterday_replies = await _count_chat_replies(db, user, yesterday)
    return ok(
        {
            "total_accounts": total_accounts,
            "active_accounts": active_accounts,
            "total_keywords": total_keywords,
            "total_orders": total_orders,
            "today_reply_count": today_replies,
            "yesterday_reply_count": yesterday_replies,
            "account_limit": None,
            "used_account_count": total_accounts,
            "remaining_account_count": (
                max(int(user.get("account_limit")) - total_accounts, 0)
                if user.get("account_limit") is not None else None
            ),
            "online_account_count": online_count,
        },
        "账号统计查询成功",
    )


@router.get("/cookies/stats/order-trend")
async def account_order_trend(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    """返回近 30 天订单金额趋势，供旧版仪表盘图表使用。"""
    user_id = _uid(user)
    start = datetime.now(timezone.utc).replace(tzinfo=None).date() - timedelta(days=29)
    rows = (
        await db.execute(
            select(Order.created_at, Order.amount)
            .join(Account, Account.id == Order.account_id)
            .where(Account.user_id == user_id, Order.created_at >= datetime.combine(start, datetime.min.time()))
        )
    ).all()
    grouped: dict[Any, dict[str, Any]] = {}
    for offset in range(30):
        day = start + timedelta(days=offset)
        grouped[day] = {"date": day.strftime("%m-%d"), "amount": 0.0, "count": 0}
    for created_at, amount in rows:
        day = created_at.date() if created_at else None
        if day in grouped:
            grouped[day]["amount"] += float(amount or 0)
            grouped[day]["count"] += 1
    return ok({"trend": list(grouped.values())}, "订单趋势查询成功")


@router.get("/admin/stats")
async def admin_stats(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    """管理员首页统计，字段与旧版仪表盘保持一致。"""
    user_id = _uid(user)
    total_users = int((await db.execute(select(func.count()).select_from(User))).scalar_one() or 0)
    total_accounts = int((await db.execute(select(func.count()).select_from(Account))).scalar_one() or 0)
    active_accounts = int((await db.execute(select(func.count()).select_from(Account).where(Account.status == "active"))).scalar_one() or 0)
    total_keywords = await _count_keyword_rules(db, user)
    total_orders = int((await db.execute(select(func.count()).select_from(Order))).scalar_one() or 0)
    account_ids = {str(value) for value in (await db.execute(select(Account.id))).scalars().all()}
    online_account_count = len(account_ids & await _online_account_ids())
    account_settings_rows = (
        await db.execute(
            select(FeatureRecord).where(FeatureRecord.feature == "account-settings")
        )
    ).scalars().all()
    password_configured = sum(
        1
        for row in account_settings_rows
        if isinstance(row.payload, dict) and str(row.payload.get("login_password") or "").strip()
    )
    total_cards = int((await db.execute(
        select(func.count()).select_from(FeatureRecord).where(
            FeatureRecord.feature == "cards",
            FeatureRecord.owner_id == user_id,
        )
    )).scalar_one() or 0)
    today = datetime.now(timezone(timedelta(hours=8))).date()
    today_reply_count = await _count_chat_replies(db, user, today)
    yesterday_reply_count = await _count_chat_replies(db, user, today - timedelta(days=1))
    return ok({
        "total_users": total_users,
        "total_cookies": total_accounts,
        "active_cookies": active_accounts,
        "online_cookies": online_account_count,
        "password_configured": password_configured,
        "total_cards": total_cards,
        "total_keywords": total_keywords,
        "total_orders": total_orders,
        "today_reply_count": today_reply_count,
        "yesterday_reply_count": yesterday_reply_count,
        "current_user_account_limit": None,
        "current_user_used_account_count": int((await db.execute(select(func.count()).select_from(Account).where(Account.user_id == user_id))).scalar_one() or 0),
        "current_user_remaining_account_count": (
            max(int(user.get("account_limit")) - int((await db.execute(select(func.count()).select_from(Account).where(Account.user_id == user_id))).scalar_one() or 0), 0)
            if user.get("account_limit") is not None else None
        ),
    }, "管理员统计查询成功")


@router.get("/admin/stats/today")
async def admin_today_stats(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    today = datetime.now(timezone.utc).replace(tzinfo=None).date()
    start = datetime.combine(today, datetime.min.time())
    end = start + timedelta(days=1)
    today_users = int((await db.execute(select(func.count()).select_from(User).where(User.created_at >= start, User.created_at < end))).scalar_one() or 0)
    today_accounts = int((await db.execute(select(func.count()).select_from(Account).where(Account.created_at >= start, Account.created_at < end))).scalar_one() or 0)
    order_rows = (await db.execute(select(Order.amount, Order.status).where(Order.created_at >= start, Order.created_at < end))).all()
    shipped_statuses = {"shipped", "delivered", "completed"}
    return ok({
        "today_users": today_users,
        "today_accounts": today_accounts,
        "today_orders": len(order_rows),
        "today_shipped": sum(1 for _, status in order_rows if status in shipped_statuses),
        "today_pending": sum(1 for _, status in order_rows if status not in shipped_statuses),
        "today_amount": sum(float(amount or 0) for amount, _ in order_rows),
        "today_agent_orders": 0,
    }, "今日统计查询成功")


@router.post("/accounts/{account_id}/sync")
async def sync_account(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    account = await _owned_account(account_id, _owner_scope(user), db)
    data = payload or {}
    try:
        page_size = min(max(int(data.get("page_size", 20)), 1), 50)
        max_pages = min(max(int(data.get("max_pages", 100)), 1), 100)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="page_size 和 max_pages 必须是整数") from exc
    try:
        result = await sync_account_products(
            db,
            account,
            page_size=page_size,
            max_pages=max_pages,
        )
        # 商品同步同时会更新订单；只有本次真正取得同步租约后才触发
        # 卡券发货检查，避免并发同步重复扫描同一批订单。
        result["delivery"] = (
            await auto_deliver_orders(db, account)
            if result.get("status") != "skipped"
            else {"status": "skipped", "reason": "sync_in_progress"}
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"闲鱼商品同步失败：{exc}") from exc
    return ok(result, result.get("message", "账号内容同步完成"))


@router.post("/internal/accounts/{account_id}/sync")
async def internal_sync_account(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    db: AsyncSession = Depends(get_session),
):
    """供 scheduler 调用的真实商品/订单同步入口。"""
    if not x_internal_token or x_internal_token != settings.jwt_secret:
        raise HTTPException(status_code=401, detail="内部调用凭证无效")
    account = (await db.execute(select(Account).where(Account.id == account_id))).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    data = payload or {}
    mode = str(data.get("mode") or "all").lower()
    if mode not in {"all", "products", "orders"}:
        raise HTTPException(status_code=422, detail="同步模式无效")
    try:
        page_size = min(max(int(data.get("page_size", 20)), 1), 50)
        max_pages = min(max(int(data.get("max_pages", 100)), 1), 100)
        result = await sync_account_products(
            db,
            account,
            page_size=page_size,
            max_pages=max_pages,
            sync_products=mode in {"all", "products"},
            sync_orders=mode in {"all", "orders"},
        )
        if mode in {"all", "orders"}:
            result["delivery"] = (
                await auto_deliver_orders(db, account)
                if result.get("status") != "skipped"
                else {"status": "skipped", "reason": "sync_in_progress"}
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"闲鱼同步失败：{exc}") from exc
    return ok(result, result.get("message", "账号内容同步完成"))


@router.get("/items/paginated")
@router.get("/items")
@router.get("/items/")
async def list_all_account_products(
    account_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """商品管理的“全部账号”视图，汇总当前用户账号内容表中的商品。"""
    statement = _account_scope(
        select(AccountContent, Account.account_name)
        .join(Account, Account.id == AccountContent.account_id)
        .where(AccountContent.content_type == "product"),
        user,
    )
    if account_id is not None:
        statement = statement.where(AccountContent.account_id == account_id)
    total = int(
        (
            await db.execute(
                _account_scope(
                    select(func.count())
                    .select_from(AccountContent)
                    .join(Account, Account.id == AccountContent.account_id)
                    .where(AccountContent.content_type == "product"),
                    user,
                )
            )
        ).scalar_one()
        or 0
    )
    if account_id is not None:
        total = int(
            (
                await db.execute(
                    _account_scope(
                        select(func.count())
                        .select_from(AccountContent)
                        .join(Account, Account.id == AccountContent.account_id)
                        .where(AccountContent.content_type == "product", AccountContent.account_id == account_id),
                        user,
                    )
                )
            ).scalar_one()
            or 0
        )
    rows = (
        await db.execute(
            statement
            .order_by(AccountContent.synced_at.desc(), AccountContent.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return ok(
        {
            "items": [_serialize(item, account_name) for item, account_name in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
        "商品查询成功",
    )


@router.get("/accounts/{account_id}/contents")
async def list_account_contents(
    account_id: int,
    content_type: str = Query(default="product", max_length=32),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    await _owned_account(account_id, _owner_scope(user), db)
    statement = select(AccountContent).where(
        AccountContent.account_id == account_id,
        AccountContent.content_type == content_type,
    )
    rows = (
        await db.execute(
            statement
            .order_by(AccountContent.synced_at.desc(), AccountContent.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(AccountContent)
                .where(
                    AccountContent.account_id == account_id,
                    AccountContent.content_type == content_type,
                )
            )
        ).scalar_one()
        or 0
    )
    return ok({"items": [_serialize(row) for row in rows], "total": total, "page": page, "page_size": page_size}, "账号内容查询成功")


@router.post("/items")
@router.post("/items/")
async def create_item(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    data = payload or {}
    account_id = data.get("account_id")
    if account_id is None:
        account = (
            await db.execute(
                _account_scope(select(Account).order_by(Account.id.desc()).limit(1), user)
            )
        ).scalar_one_or_none()
        if account is None:
            raise HTTPException(status_code=409, detail="请先添加并接入一个账号")
        account_id = account.id
    account = await _owned_account(int(account_id), _owner_scope(user), db)
    external_id = str(data.get("external_id") or f"local-{account.id}-{datetime.now().timestamp()}")
    item = AccountContent(
        account_id=account.id,
        content_type="product",
        external_id=external_id,
        title=str(data.get("title") or "未命名商品"),
        description=data.get("description"),
        price=data.get("price"),
        stock=int(data.get("stock") or 0),
        images=data.get("images") if isinstance(data.get("images"), list) else [],
        status=str(data.get("status") or "draft"),
        payload=data.get("payload") if isinstance(data.get("payload"), dict) else None,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return ok(_serialize(item, account.account_name), "商品已创建")


@router.put("/items/account/{account_id}/{external_id}")
async def update_external_item(
    account_id: int,
    external_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    row = await _owned_external_item(account_id, external_id, _owner_scope(user), db)
    values = payload or {}
    if "title" in values or "item_title" in values:
        row.title = str(values.get("title") or values.get("item_title") or row.title)
    if "description" in values or "item_detail" in values or "desc" in values:
        row.description = values.get("description") or values.get("item_detail") or values.get("desc")
    if "price" in values:
        row.price = values["price"]
    if "stock" in values:
        row.stock = int(values["stock"] or 0)
    if "status" in values:
        row.status = str(values["status"])
    await _save_external_payload(row, {key: value for key, value in values.items() if key not in {"title", "item_title", "description", "item_detail", "desc", "price", "stock", "status"}}, db)
    return ok(_serialize(row), "商品已更新")


@router.post("/items/account/{account_id}/batch-offline")
async def batch_offline_external_items(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    account = await _owned_account(account_id, _owner_scope(user), db)
    item_ids = list(dict.fromkeys(str(item).strip() for item in (payload or {}).get("item_ids", []) if str(item).strip()))
    if not item_ids:
        raise HTTPException(status_code=422, detail="请选择要下架的商品")
    if not account.cookie:
        raise HTTPException(status_code=409, detail="该账号未登录，无法下架商品")
    existing = list((await db.execute(select(AccountContent).where(AccountContent.account_id == account_id, AccountContent.content_type == "product", AccountContent.external_id.in_(item_ids)))).scalars().all())
    existing_ids = {row.external_id for row in existing}
    missing = [item_id for item_id in item_ids if item_id not in existing_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"本地商品不存在：{'、'.join(missing[:5])}")
    try:
        result = await offline_items(cookies=account.cookie, account_id=str(account.goofish_id or account.id), item_ids=item_ids)
    except XianyuPlatformError as exc:
        raise HTTPException(status_code=502, detail=f"闲鱼商品下架失败：{exc}") from exc
    await _persist_platform_cookie(account, str(result.get("cookies_str") or account.cookie), db)
    successful = {str(item["item_id"]) for item in result.get("results", []) if item.get("success")}
    for row in existing:
        if row.external_id in successful:
            row.status = "off_sale"
    await db.commit()
    success_count = int(result.get("success_count") or 0)
    message = f"已下架 {success_count} 个商品"
    if result.get("fail_count"):
        message += f"，{int(result['fail_count'])} 个失败"
    return ok({"results": result.get("results", []), "suc_count": success_count, "success_count": success_count, "fail_count": int(result.get("fail_count") or 0)}, message)


@router.post("/items/account/{account_id}/batch-delete-platform")
async def batch_delete_platform_items(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    account = await _owned_account(account_id, _owner_scope(user), db)
    item_ids = list(dict.fromkeys(str(item).strip() for item in (payload or {}).get("item_ids", []) if str(item).strip()))
    if not item_ids:
        raise HTTPException(status_code=422, detail="请选择要删除的闲鱼商品")
    if not account.cookie:
        raise HTTPException(status_code=409, detail="该账号未登录，无法删除闲鱼商品")
    existing = list((await db.execute(select(AccountContent).where(AccountContent.account_id == account_id, AccountContent.content_type == "product", AccountContent.external_id.in_(item_ids)))).scalars().all())
    existing_ids = {row.external_id for row in existing}
    missing = [item_id for item_id in item_ids if item_id not in existing_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"本地商品不存在：{'、'.join(missing[:5])}")
    try:
        result = await delete_items(cookies=account.cookie, account_id=str(account.goofish_id or account.id), item_ids=item_ids)
    except XianyuPlatformError as exc:
        raise HTTPException(status_code=502, detail=f"闲鱼平台删除失败：{exc}") from exc
    await _persist_platform_cookie(account, str(result.get("cookies_str") or account.cookie), db)
    await db.commit()
    success_count = int(result.get("success_count") or 0)
    message = f"闲鱼平台删除成功 {success_count} 个商品"
    if result.get("fail_count"):
        message += f"，{int(result['fail_count'])} 个失败"
    if success_count == 0:
        raise HTTPException(status_code=502, detail=message)
    return ok({"results": result.get("results", []), "success_count": success_count, "fail_count": int(result.get("fail_count") or 0)}, message)


@router.post("/items/batch-offline")
async def legacy_batch_offline_items(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """旧版商品页批量下架入口，转入同一套真实平台适配器。"""
    data = payload or {}
    account = await _account_by_identifier(data.get("cookie_id") or data.get("account_id"), user, db)
    return await batch_offline_external_items(account_id=account.id, payload={"item_ids": data.get("item_ids") or []}, user=user, db=db)


@router.post("/items/batch-delete-xianyu")
async def legacy_batch_delete_xianyu_items(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """旧版商品页批量删除闲鱼平台商品入口。"""
    data = payload or {}
    account = await _account_by_identifier(data.get("cookie_id") or data.get("account_id"), user, db)
    return await batch_delete_platform_items(account_id=account.id, payload={"item_ids": data.get("item_ids") or []}, user=user, db=db)


@router.delete("/items/delete")
async def delete_item_unified(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """兼容旧版统一删除入口，删除的是本地同步记录。"""
    data = payload or {}
    external_id = str(data.get("item_id") or data.get("external_id") or "").strip()
    if not external_id:
        raise HTTPException(status_code=422, detail="商品ID不能为空")
    account_value = str(data.get("cookie_id") or data.get("account_id") or "").strip()
    statement = select(AccountContent).join(Account, Account.id == AccountContent.account_id).where(
        AccountContent.content_type == "product", AccountContent.external_id == external_id,
    )
    if account_value:
        account = await _account_by_identifier(account_value, user, db)
        statement = statement.where(AccountContent.account_id == account.id)
    elif not _is_admin(user):
        statement = statement.where(Account.user_id == _uid(user))
    rows = list((await db.execute(statement)).scalars().all())
    if len(rows) > 1 and not account_value:
        raise HTTPException(status_code=409, detail="该商品属于多个账号，请指定账号后再删除")
    if not rows:
        raise HTTPException(status_code=404, detail="商品不存在")
    await db.delete(rows[0]); await db.commit()
    return ok({"id": rows[0].id, "external_id": external_id, "deleted": True}, "商品已删除")


@router.delete("/items/batch")
async def batch_delete_local_items(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """兼容旧版批量删除入口，逐条执行本地商品删除并返回明细。"""
    entries = (payload or {}).get("items") or []
    if not isinstance(entries, list) or not entries:
        raise HTTPException(status_code=422, detail="请选择要删除的商品")
    results = []
    for entry in entries:
        if not isinstance(entry, dict):
            results.append({"success": False, "message": "商品参数无效"}); continue
        try:
            result = await delete_item_unified(entry, user, db)
            results.append({"success": True, "item_id": entry.get("item_id"), "message": "商品已删除"})
        except HTTPException as exc:
            results.append({"success": False, "item_id": entry.get("item_id"), "message": str(exc.detail)})
    success_count = sum(1 for item in results if item["success"])
    await db.commit()
    if success_count == 0:
        raise HTTPException(status_code=404, detail="未能删除任何商品")
    return ok({"results": results, "success_count": success_count, "fail_count": len(results) - success_count}, f"已删除 {success_count} 个商品")


@router.delete("/items/{account_identifier}/{external_id}")
async def delete_item_legacy_path(
    account_identifier: str,
    external_id: str,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """兼容旧版 /items/{cookie_id}/{item_id} 删除路径。"""
    account = await _account_by_identifier(account_identifier, user, db)
    return await delete_item_unified({"account_id": account.id, "item_id": external_id}, user, db)


@router.put("/items/account/{account_id}/{external_id}/multi-quantity-delivery")
async def update_external_multi_quantity(
    account_id: int,
    external_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    row = await _owned_external_item(account_id, external_id, _owner_scope(user), db)
    await _save_external_payload(row, {"multi_quantity_delivery": bool((payload or {}).get("multi_quantity_delivery"))}, db)
    return ok(_serialize(row), "多数量发货设置已更新")


@router.put("/items/account/{account_id}/{external_id}/multi-spec")
async def update_external_multi_spec(
    account_id: int,
    external_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    row = await _owned_external_item(account_id, external_id, _owner_scope(user), db)
    await _save_external_payload(row, {"is_multi_spec": bool((payload or {}).get("is_multi_spec"))}, db)
    return ok(_serialize(row), "多规格设置已更新")


@router.post("/items/{account_id}/{external_id}/default-reply/upload-image")
async def upload_external_default_reply_image(
    account_id: int,
    external_id: str,
    image: UploadFile = File(...),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """上传并保存商品默认回复图片，不能再落到旧版等待记录。"""
    row = await _owned_external_item(account_id, external_id, _owner_scope(user), db)
    upload_dir = Path(settings.static_dir) / "uploads" / "item-replies"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(image.filename or "reply-image.bin").name
    target = upload_dir / f"{uuid4().hex}_{safe_name}"
    content = await image.read()
    target.write_bytes(content)
    image_url = f"/static/uploads/item-replies/{target.name}"
    current = _payload(row)
    reply = dict(current.get("default_reply") or {})
    reply["reply_image"] = image_url
    current.update({"default_reply": reply, "has_default_reply": True})
    row.payload = current
    await db.commit()
    return ok({"image_url": image_url, "item_id": external_id}, "商品默认回复图片已上传")


@router.post("/items/{account_id}/batch-default-reply/upload-image")
async def upload_batch_default_reply_image(
    account_id: int,
    image: UploadFile = File(...),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    await _owned_account(account_id, _owner_scope(user), db)
    upload_dir = Path(settings.static_dir) / "uploads" / "item-replies"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(image.filename or "reply-image.bin").name
    target = upload_dir / f"{uuid4().hex}_{safe_name}"
    target.write_bytes(await image.read())
    return ok({"image_url": f"/static/uploads/item-replies/{target.name}", "account_id": account_id}, "批量默认回复图片已上传")


@router.delete("/items/account/{account_id}/{external_id}")
async def delete_external_item(account_id: int, external_id: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _owned_external_item(account_id, external_id, _owner_scope(user), db)
    await db.delete(row)
    await db.commit()
    return ok({"external_id": external_id, "deleted": True}, "商品已删除")


@router.get("/items/account/{account_id}/{external_id}/default-reply")
async def get_external_default_reply(account_id: int, external_id: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _owned_external_item(account_id, external_id, _owner_scope(user), db)
    value = _payload(row).get("default_reply") or {}
    return ok({"item_id": external_id, **value}, "商品默认回复查询成功")


@router.put("/items/account/{account_id}/{external_id}/default-reply")
async def update_external_default_reply(account_id: int, external_id: str, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _owned_external_item(account_id, external_id, _owner_scope(user), db)
    value = payload or {}
    data = {"default_reply": value, "has_default_reply": bool(value.get("reply_content") or value.get("reply_image") or value.get("reply_type") == "api"), "default_reply_enabled": bool(value.get("enabled", True))}
    await _save_external_payload(row, data, db)
    return ok(_serialize(row), "商品默认回复已保存")


@router.delete("/items/account/{account_id}/{external_id}/default-reply")
async def delete_external_default_reply(account_id: int, external_id: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _owned_external_item(account_id, external_id, _owner_scope(user), db)
    data = _payload(row)
    data.pop("default_reply", None)
    data.update({"has_default_reply": False, "default_reply_enabled": False})
    row.payload = data
    await db.commit()
    return ok({"item_id": external_id, "deleted": True}, "商品默认回复已删除")


@router.get("/items/account/{account_id}/{external_id}/ai-prompt")
async def get_external_ai_prompt(account_id: int, external_id: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _owned_external_item(account_id, external_id, _owner_scope(user), db)
    data = _payload(row)
    return ok({"item_id": external_id, "ai_prompt": data.get("ai_prompt", "")}, "商品 AI 提示词查询成功")


@router.put("/items/account/{account_id}/{external_id}/ai-prompt")
async def update_external_ai_prompt(account_id: int, external_id: str, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _owned_external_item(account_id, external_id, _owner_scope(user), db)
    prompt = str((payload or {}).get("ai_prompt", ""))
    await _save_external_payload(row, {"ai_prompt": prompt, "has_ai_prompt": bool(prompt.strip())}, db)
    return ok(_serialize(row), "商品 AI 提示词已保存")


@router.get("/items/{item_id}")
async def get_item(item_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = (
        await db.execute(
            select(AccountContent, Account.account_name)
            .join(Account, Account.id == AccountContent.account_id)
            .where(AccountContent.id == item_id, Account.user_id == _uid(user), AccountContent.content_type == "product")
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    return ok(_serialize(row[0], row[1]), "商品查询成功")


@router.put("/items/{item_id}")
async def update_item(item_id: int, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = (
        await db.execute(
            select(AccountContent)
            .join(Account, Account.id == AccountContent.account_id)
            .where(AccountContent.id == item_id, Account.user_id == _uid(user), AccountContent.content_type == "product")
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    editable = {"title", "description", "price", "stock", "images", "status"}
    for key, value in (payload or {}).items():
        if key in editable:
            setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    return ok(_serialize(row), "商品已更新")


@router.delete("/items/{item_id}")
async def delete_item(item_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = (
        await db.execute(
            select(AccountContent)
            .join(Account, Account.id == AccountContent.account_id)
            .where(AccountContent.id == item_id, Account.user_id == _uid(user), AccountContent.content_type == "product")
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    await db.delete(row)
    await db.commit()
    return ok({"id": item_id, "deleted": True}, "商品已删除")
