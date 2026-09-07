# -*- coding: utf-8 -*-
"""账号管理的真实数据链路：新增、列表和状态变更。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from common.config import settings
from common.db.session import get_session
from common.models.account_cookies import AccountCookie
from common.models.account_contents import AccountContent, AccountSyncState
from common.models.accounts import Account
from common.models.catalog import PublishLog
from common.models.extended import PublishJob
from common.models.orders import Order
from common.models.products import Product
from common.models.rules import DefaultReply
from common.models.feature_records import FeatureRecord
from common.models.chat import ChatMessageRecord
from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.services.account_settings import load_account_settings_map, load_platform_ai_settings
from backend.app.services.entitlements import FEATURE_ACCOUNT, finalize_quota, reserve_quota
from common.services.account_identity import display_account_name, extract_account_nickname, is_generated_account_name
from common.services.cloud_auth import CloudAuthError, cloud_auth_request, cloud_auth_url

router = APIRouter(prefix="/api/v1/accounts", tags=["账号管理"])


class AccountCreate(BaseModel):
    account_name: str = Field(min_length=1, max_length=64)
    goofish_id: str | None = Field(default=None, max_length=64)
    proxy: str | None = Field(default=None, max_length=256)
    cookie: str | None = None


class AccountUpdate(BaseModel):
    account_name: str | None = Field(default=None, min_length=1, max_length=64)
    goofish_id: str | None = Field(default=None, max_length=64)
    proxy: str | None = Field(default=None, max_length=256)
    status: str | None = Field(default=None, max_length=16)
    cookie: str | None = None


def serialize(account: Account, account_settings: dict | None = None) -> dict:
    data = {
        "id": account.id,
        "account_name": display_account_name(account.account_name, account.goofish_id, account.cookie),
        "goofish_id": account.goofish_id,
        "proxy": account.proxy,
        "status": account.status,
        "cookie_expire_at": account.cookie_expire_at.isoformat() if account.cookie_expire_at else None,
        "created_at": account.created_at.isoformat() if account.created_at else None,
    }
    settings = account_settings or {}
    # 将账号列表需要的开关一并返回，确保按钮操作后刷新页面仍保持真实状态。
    for key in (
        "ai_enabled", "scheduled_redelivery", "scheduled_rate", "auto_polish",
        "auto_confirm", "confirm_before_send", "send_before_confirm", "only_send_card",
        "auto_red_flower", "ai_reply_block_ordered_users", "delivery_disabled",
        "delivery_disabled_reason", "pause_duration", "message_expire_time",
        "reply_delay_seconds", "username", "show_browser",
    ):
        if key in settings:
            data[key] = settings[key]
    data["has_password"] = bool(settings.get("login_password"))
    return data


def _uid(user: dict) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _is_admin(user: dict) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _account_scope(statement, user: dict, account_id: int | None = None):
    if account_id is not None:
        statement = statement.where(Account.id == account_id)
    # 管理员权限只用于后台管理能力，不扩大业务账号数据范围。
    # 账号及其下属商品、订单、消息必须始终属于当前登录用户。
    return statement.where(Account.user_id == _uid(user))


async def _settings_for_accounts(session: AsyncSession, user: dict, accounts: list[Account]) -> dict[int, dict]:
    if not accounts:
        return {}
    if not _is_admin(user):
        result = await load_account_settings_map(session, _uid(user), [item.id for item in accounts])
        platform_settings = await load_platform_ai_settings(session, _uid(user))
        for item in accounts:
            result.setdefault(int(item.id), {})["ai_enabled"] = bool(platform_settings.get("ai_enabled"))
        return result
    result: dict[int, dict] = {}
    for owner_id in {int(item.user_id) for item in accounts}:
        owner_accounts = [item for item in accounts if int(item.user_id) == owner_id]
        result.update(await load_account_settings_map(session, owner_id, [item.id for item in owner_accounts]))
        platform_settings = await load_platform_ai_settings(session, owner_id)
        for item in owner_accounts:
            result.setdefault(int(item.id), {})["ai_enabled"] = bool(platform_settings.get("ai_enabled"))
    return result


async def _content_counts(session: AsyncSession, accounts: list[Account]) -> dict[int, dict[str, int]]:
    """统计账号管理列表需要的真实关键词、过滤词和今日回复数量。"""
    if not accounts:
        return {}
    account_index = {
        (str(account.user_id), str(account.id)): int(account.id)
        for account in accounts
    }
    owners = {int(account.user_id) for account in accounts}
    result = {
        int(account.id): {"keywordCount": 0, "filter_count": 0, "today_reply_count": 0}
        for account in accounts
    }
    feature_rows = list(
        (
            await session.execute(
                select(FeatureRecord).where(
                    FeatureRecord.owner_id.in_(owners),
                    FeatureRecord.feature.in_(["keywords-with-item-id", "message-filters"]),
                    FeatureRecord.status == "active",
                )
            )
        ).scalars().all()
    )
    for row in feature_rows:
        payload = row.payload or {}
        account_id = account_index.get((str(row.owner_id), str(payload.get("account_id") or "")))
        if account_id is None:
            continue
        if row.feature == "keywords-with-item-id":
            result[account_id]["keywordCount"] += 1
        elif row.feature == "message-filters":
            result[account_id]["filter_count"] += 1

    # 实时聊天消息统一落在 ChatMessageRecord；必须按平台消息时间统计，
    # 不能用 created_at，因为打开聊天页补存历史消息时 created_at 会变成当前时间。
    beijing = timezone(timedelta(hours=8))
    start = datetime.now(beijing).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    reply_rows = (
        await session.execute(
            select(ChatMessageRecord.account_id, func.count(ChatMessageRecord.id))
            .where(
                ChatMessageRecord.account_id.in_([int(account.id) for account in accounts]),
                ChatMessageRecord.is_self.is_(True),
                ChatMessageRecord.message_time >= int(start.timestamp() * 1000),
                ChatMessageRecord.message_time < int(end.timestamp() * 1000),
            )
            .group_by(ChatMessageRecord.account_id)
        )
    ).all()
    for account_id, count in reply_rows:
        if int(account_id) in result:
            result[int(account_id)]["today_reply_count"] = int(count or 0)
    return result


async def _count(session: AsyncSession, model: type, column, value: int) -> int:
    result = await session.execute(select(func.count()).select_from(model).where(column == value))
    return int(result.scalar_one() or 0)


async def _connection_status(account_id: int) -> dict:
    """读取连接服务状态；连接服务不可用时不影响账号详情页。"""
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            response = await client.get(
                f"{settings.websocket_service_url}/internal/accounts/{account_id}/status",
                headers={"X-Internal-Token": settings.jwt_secret},
            )
        payload = response.json()
        return payload.get("data") or {"status": "unknown"}
    except (httpx.HTTPError, ValueError):
        return {"status": "unavailable", "cookie_loaded": False}


async def _online_account_ids() -> set[str]:
    """读取连接服务真正建立的闲鱼账号连接集合。"""
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            response = await client.get(
                f"{settings.websocket_service_url.rstrip('/')}/internal/accounts/connection-stats",
                headers={"X-Internal-Token": settings.jwt_secret},
            )
        if not response.is_success:
            return set()
        payload = response.json()
        values = (payload.get("data") or {}).get("connected_account_ids") or []
        return {str(value) for value in values}
    except (httpx.HTTPError, ValueError, TypeError):
        return set()


async def _notify_runtime(account: Account, action: str) -> dict:
    """同步账号生命周期与真实连接服务。"""
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(
                f"{settings.websocket_service_url.rstrip('/')}/internal/accounts/{account.id}/{action}",
                json={"cookie_value": account.cookie or "", "user_id": int(account.user_id)},
                headers={"X-Internal-Token": settings.jwt_secret},
            )
        payload = response.json()
        if response.is_success and payload.get("success"):
            return payload.get("data") or {"status": "accepted"}
        return {"status": "failed", "error": payload.get("message", "连接服务拒绝请求")}
    except (httpx.HTTPError, ValueError) as exc:
        return {"status": "unavailable", "error": str(exc)}


def _cloud_token(user: dict) -> str:
    token = str(user.get("cloud_session_token") or "").strip()
    if not cloud_auth_url() or not token:
        raise HTTPException(status_code=409, detail="当前未启用云端账号同步")
    return token


async def _cloud_request(action: str, payload: dict, user: dict) -> dict:
    try:
        return await cloud_auth_request(action, payload, _cloud_token(user)) or {}
    except CloudAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/cloud-sessions")
async def list_cloud_sessions(user=Depends(get_current_user)):
    remote = await _cloud_request("list_account_sessions", {}, user)
    return ok({"items": remote.get("items") or []}, "云端会话查询成功")


@router.post("/{account_id}/cloud-sync")
async def sync_cloud_session(
    account_id: int,
    device_id: str = Header(default="", alias="X-Device-ID"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    account = (await session.execute(_account_scope(select(Account), user, account_id))).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    if not account.cookie:
        raise HTTPException(status_code=409, detail="该账号尚未登录，无法同步")
    result = await _cloud_request("sync_account_session", {
        "account_key": account.goofish_id or f"local:{account.id}",
        "account_name": account.account_name,
        "device_id": (device_id or "unknown-device")[:128],
        "session_payload": {"cookie": account.cookie, "goofish_id": account.goofish_id, "account_name": account.account_name},
        "metadata": {"status": account.status, "cookie_expire_at": account.cookie_expire_at.isoformat() if account.cookie_expire_at else None},
    }, user)
    return ok(result.get("session") or {}, "闲鱼登录会话已加密同步")


@router.post("/cloud-sessions/{session_id}/restore")
async def restore_cloud_session(
    session_id: int,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    remote = await _cloud_request("get_account_session", {"session_id": session_id}, user)
    payload = (remote.get("session") or {}).get("session_payload") or {}
    cookie = str(payload.get("cookie") or "").strip()
    account_key = str(payload.get("goofish_id") or (remote.get("session") or {}).get("account_key") or "").strip()
    if not cookie or not account_key:
        raise HTTPException(status_code=502, detail="云端会话内容不完整")
    account = (await session.execute(_account_scope(select(Account), user))).scalars().all()
    target = next((item for item in account if item.goofish_id == account_key), None)
    if target is None:
        reservation = await reserve_quota(session, user, FEATURE_ACCOUNT, resource_key=account_key, idempotency_key=f"cloud-restore:{account_key}")
        target = Account(user_id=int(user.get("sub", 1)), account_name=str(payload.get("account_name") or account_key)[:64], goofish_id=account_key, cookie=cookie, status="active")
        session.add(target)
        finalize_quota(reservation)
    else:
        target.cookie = cookie
        target.status = "active"
    await session.commit()
    await session.refresh(target)
    runtime = await _notify_runtime(target, "start")
    return ok({**serialize(target), "online": bool(runtime.get("is_connected")), "runtime": runtime}, "闲鱼登录会话已恢复")


@router.delete("/cloud-sessions/{session_id}")
async def delete_cloud_session(session_id: int, user=Depends(get_current_user)):
    await _cloud_request("delete_account_session", {"session_id": session_id}, user)
    return ok({"deleted": True}, "云端会话已撤销")


@router.get("")
@router.get("/")
async def list_accounts(user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(_account_scope(select(Account).order_by(Account.id.desc()), user))
    accounts = result.scalars().all()
    settings_map = await _settings_for_accounts(session, user, accounts)
    content_counts = await _content_counts(session, accounts)
    online_ids = await _online_account_ids()
    items = []
    names_changed = False
    for item in accounts:
        nickname = extract_account_nickname(item.cookie)
        if nickname and is_generated_account_name(item.account_name, item.goofish_id):
            item.account_name = nickname
            names_changed = True
        serialized = serialize(item, settings_map.get(item.id))
        serialized.update(content_counts.get(int(item.id), {}))
        # 在线只认连接服务返回的真实连接，不把“账号已启用”误报为在线。
        serialized["online"] = str(item.id) in online_ids
        items.append(serialized)
    if names_changed:
        await session.commit()
    return ok({"items": items, "total": len(items), "page": 1, "page_size": len(items)})


@router.post("")
@router.post("/")
async def create_account(payload: AccountCreate, user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    reservation = await reserve_quota(
        session,
        user,
        FEATURE_ACCOUNT,
        resource_key=payload.goofish_id or payload.account_name,
        idempotency_key=f"account:create:{payload.goofish_id or payload.account_name}",
    )
    account = Account(
        user_id=int(user.get("sub", 1)),
        account_name=payload.account_name.strip(),
        goofish_id=payload.goofish_id.strip() if payload.goofish_id else None,
        proxy=payload.proxy.strip() if payload.proxy else None,
        status="inactive",
        cookie=payload.cookie.strip() if payload.cookie else None,
    )
    nickname = extract_account_nickname(account.cookie)
    if nickname and is_generated_account_name(account.account_name, account.goofish_id):
        account.account_name = nickname
    session.add(account)
    try:
        finalize_quota(reservation)
        await session.commit()
        await session.refresh(account)
    except SQLAlchemyError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="账号保存失败，请检查账号标识是否重复") from exc
    if account.cookie:
        session.add(AccountCookie(account_id=account.id, cookie_value=account.cookie, status="active"))
        await session.commit()
    return ok(serialize(account), "账号已添加")


@router.patch("/{account_id}/status")
async def change_account_status(account_id: int, status: str, user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if status not in {"active", "inactive", "paused", "expired"}:
        raise HTTPException(status_code=422, detail="不支持的账号状态")
    account = (await session.execute(_account_scope(select(Account), user, account_id))).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    account.status = status
    await session.commit()
    runtime = await _notify_runtime(account, "start" if status == "active" else "stop")
    return ok({**serialize(account), "online": bool(runtime.get("is_connected")), "runtime": runtime}, "账号状态已更新")


@router.get("/{account_id}/detail")
async def get_account_detail(account_id: int, user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """返回账号可展示的完整摘要，不返回明文 Cookie。"""
    account = (await session.execute(_account_scope(select(Account), user, account_id))).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    latest_cookie = (
        await session.execute(
            select(AccountCookie)
            .where(AccountCookie.account_id == account.id)
            .order_by(AccountCookie.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    connection = await _connection_status(account.id)
    sync_state = (
        await session.execute(
            select(AccountSyncState).where(AccountSyncState.account_id == account.id)
        )
    ).scalar_one_or_none()
    synced_product_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(AccountContent)
                .where(
                    AccountContent.account_id == account.id,
                    AccountContent.content_type == "product",
                )
            )
        ).scalar_one()
        or 0
    )
    recent_products = (
        await session.execute(
            select(AccountContent)
            .where(
                AccountContent.account_id == account.id,
                AccountContent.content_type == "product",
            )
            .order_by(AccountContent.synced_at.desc(), AccountContent.id.desc())
            .limit(5)
        )
        ).scalars().all()
    content_counts = await _content_counts(session, [account])
    content = {
        "messages": await _count(session, ChatMessageRecord, ChatMessageRecord.account_id, account.id),
        "orders": await _count(session, Order, Order.account_id, account.id),
        "products": synced_product_count + await _count(session, Product, Product.account_id, account.id),
        "publish_jobs": await _count(session, PublishJob, PublishJob.account_id, account.id),
        "publish_logs": await _count(session, PublishLog, PublishLog.account_id, account.id),
        "keyword_rules": content_counts.get(int(account.id), {}).get("keywordCount", 0),
        "default_replies": await _count(session, DefaultReply, DefaultReply.owner_id, account.user_id),
    }
    account_settings = await _settings_for_accounts(session, user, [account])
    return ok({
        "account": serialize(account, account_settings.get(account.id)),
        "login_state": {
            "has_cookie": bool(account.cookie),
            "cookie_length": len(account.cookie or ""),
            "cookie_records": await _count(session, AccountCookie, AccountCookie.account_id, account.id),
            "last_cookie_at": latest_cookie.created_at.isoformat() if latest_cookie and latest_cookie.created_at else None,
            "expires_at": (
                latest_cookie.expires_at.isoformat()
                if latest_cookie and latest_cookie.expires_at
                else account.cookie_expire_at.isoformat() if account.cookie_expire_at else None
            ),
        },
        "connection": connection,
        "content": content,
        "sync": {
            "status": sync_state.status if sync_state else "never",
            "last_started_at": sync_state.last_started_at.isoformat() if sync_state and sync_state.last_started_at else None,
            "last_finished_at": sync_state.last_finished_at.isoformat() if sync_state and sync_state.last_finished_at else None,
            "last_error": sync_state.last_error if sync_state else None,
            "products_count": sync_state.products_count if sync_state else synced_product_count,
            "orders_count": sync_state.orders_count if sync_state else content["orders"],
            "messages_count": sync_state.messages_count if sync_state else content["messages"],
            "last_result": sync_state.last_result if sync_state else None,
        },
        "recent_products": [
            {
                "id": item.id,
                "external_id": item.external_id,
                "title": item.title,
                "price": float(item.price) if item.price is not None else None,
                "status": item.status,
                "synced_at": item.synced_at.isoformat() if item.synced_at else None,
            }
            for item in recent_products
        ],
    }, "账号详情查询成功")


@router.get("/{account_id}")
async def get_account(account_id: int, user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    account = (await session.execute(_account_scope(select(Account), user, account_id))).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    account_settings = await _settings_for_accounts(session, user, [account])
    return ok(serialize(account, account_settings.get(account.id)))


@router.put("/{account_id}")
async def update_account(account_id: int, payload: AccountUpdate, user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if payload.status is not None and payload.status not in {"active", "inactive", "paused", "expired"}:
        raise HTTPException(status_code=422, detail="不支持的账号状态")
    account = (await session.execute(_account_scope(select(Account), user, account_id))).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    values = payload.model_dump(exclude_unset=True)
    if "account_name" in values:
        account.account_name = values["account_name"].strip()
    if "goofish_id" in values:
        account.goofish_id = values["goofish_id"].strip() if values["goofish_id"] else None
    if "proxy" in values:
        account.proxy = values["proxy"].strip() if values["proxy"] else None
    if "status" in values:
        account.status = values["status"]
    if "cookie" in values:
        account.cookie = values["cookie"].strip() if values["cookie"] else None
        if account.cookie:
            session.add(AccountCookie(account_id=account.id, cookie_value=account.cookie, status="active"))
    try:
        await session.commit()
        await session.refresh(account)
    except SQLAlchemyError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="账号更新失败，请检查账号标识是否重复") from exc
    runtime = None
    if "cookie" in values or "status" in values:
        runtime = await _notify_runtime(account, "start" if account.status == "active" and account.cookie else "stop")
    response = serialize(account)
    if runtime is not None:
        response.update({"online": bool(runtime.get("is_connected")), "runtime": runtime})
    return ok(response, "账号已更新")


@router.delete("/{account_id}")
async def delete_account(account_id: int, user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    account = (await session.execute(_account_scope(select(Account), user, account_id))).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    owner_id = int(account.user_id)
    await _notify_runtime(account, "stop")
    # 账号设置和 Cookie 轮换记录没有外键级联，删除账号时同步清理，避免残留脏数据。
    await session.execute(
        delete(FeatureRecord).where(
            FeatureRecord.owner_id == owner_id,
            FeatureRecord.feature == "account-settings",
            FeatureRecord.external_id == str(account_id),
        )
    )
    await session.execute(delete(AccountCookie).where(AccountCookie.account_id == account_id))
    await session.execute(delete(AccountContent).where(AccountContent.account_id == account_id))
    await session.execute(delete(AccountSyncState).where(AccountSyncState.account_id == account_id))
    await session.delete(account)
    await session.commit()
    return ok({"id": account_id, "deleted": True}, "账号已删除")
