# -*- coding: utf-8 -*-
"""在线聊天账号连接与浏览器 WebSocket 握手。"""
from __future__ import annotations

import httpx
import json
import asyncio
import secrets
import time
from pathlib import Path
from uuid import uuid4
from typing import Any
from fastapi import APIRouter, Body, Depends, File, Form, Header, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.core.security import decode_access_token
from common.config import settings
from common.db.session import async_session_maker, get_session
from common.models.accounts import Account
from common.models.account_cookies import AccountCookie
from common.models.account_contents import AccountContent
from common.models.chat import ChatMessageRecord
from common.models.orders import Order
from common.models.feature_records import FeatureRecord
from common.models.notification_channels import MessageNotificationBinding, NotificationChannel
from common.models.notifications import Notification
from common.utils.xianyu_message_parser import interpret_content, load_content_json
from common.utils.xianyu_push import extract_item_id, extract_order_id
from backend.app.api.routes.notifications import send_channel
from backend.app.services.auto_reply_service import dispatch_auto_reply
from backend.app.services.card_delivery import PAYMENT_EVENT_PARTS, auto_deliver_live_event
from backend.app.services.order_status import apply_live_order_status
from common.services.order_status import merge_order_status, status_from_chat_text
from common.services.xianyu_platform import XianyuPlatformError, official_blacklist
from common.services.goofish_mtop import mtop_call
from common.services.account_identity import display_account_name, extract_account_nickname, is_generated_account_name

router = APIRouter(prefix="/api/v1/chat-new", tags=["在线聊天"])
_browser_clients: dict[int, set[WebSocket]] = {}
_browser_clients_lock = asyncio.Lock()


def _uid(user: dict) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _is_admin(user: dict) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _parse_cookie(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in str(value or "").split(";"):
        key, separator, item = part.strip().partition("=")
        if separator and key:
            result[key] = item
    return result


async def _online_ids() -> set[str]:
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            response = await client.get(
                f"{settings.websocket_service_url.rstrip('/')}/internal/accounts/connection-stats",
                headers={"X-Internal-Token": settings.jwt_secret},
            )
        if not response.is_success:
            return set()
        payload = response.json()
        return {str(item) for item in (payload.get("data") or {}).get("connected_account_ids") or []}
    except (httpx.HTTPError, ValueError, TypeError):
        return set()


async def _get_account(account_id: int, user: dict, db: AsyncSession) -> Account:
    statement = select(Account).where(Account.id == account_id, Account.user_id == _uid(user))
    account = (await db.execute(statement)).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return account


async def _runtime_call(account: Account, action: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(
                f"{settings.websocket_service_url.rstrip('/')}/internal/accounts/{account.id}/{action}",
                json={"cookie_value": account.cookie or "", "user_id": int(account.user_id)},
                headers={"X-Internal-Token": settings.jwt_secret},
            )
        payload = response.json()
        if response.is_success and payload.get("success"):
            return payload.get("data") or {}
        raise HTTPException(status_code=409, detail=payload.get("message", "连接服务拒绝请求"))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"连接服务不可用：{exc}") from exc


async def _runtime_query(account_id: int, path: str, payload: dict[str, object]) -> dict:
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(
                f"{settings.websocket_service_url.rstrip('/')}/internal/chat/{account_id}/{path}",
                json=payload,
                headers={"X-Internal-Token": settings.jwt_secret},
            )
        if not response.is_success:
            return {}
        result = response.json()
        return result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else {}
    except (httpx.HTTPError, ValueError, TypeError):
        return {}


def _strip(value: object) -> str:
    return str(value or "").split("@", 1)[0]


def _message_from_history(model: dict, myid: str) -> dict | None:
    try:
        message = model.get("message") or {}
        extension = message.get("extension") or {}
        if isinstance(extension, str):
            extension = json.loads(extension)
        custom = ((message.get("content") or {}).get("custom") or {})
        data = custom.get("data") or ""
        decoded = load_content_json(data) if data else None
        text, images, msg_type = interpret_content(decoded or {})
        if not text and not images:
            text = str(custom.get("summary") or "[系统消息]")
        sender_id = _strip(extension.get("senderUserId") if isinstance(extension, dict) else "")
        return {
            "messageId": str(message.get("messageId") or ""),
            "senderId": sender_id,
            "senderName": str(extension.get("reminderTitle") or "" if isinstance(extension, dict) else ""),
            "isSelf": sender_id == myid,
            "type": msg_type if msg_type in {"text", "image"} else "text",
            "text": text,
            "images": images,
            "time": int(message.get("createAt") or message.get("time") or 0),
            "itemId": extract_item_id(model),
            "orderId": extract_order_id(model),
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _conversation_from_live(value: dict, myid: str) -> dict | None:
    try:
        conv = value.get("singleChatUserConversation", value)
        single = conv.get("singleChatConversation", {})
        raw_cid = str(single.get("cid") or "")
        cid = _strip(raw_cid)
        if not cid:
            return None
        first, second = _strip(single.get("pairFirst")), _strip(single.get("pairSecond"))
        other = second if first == myid else first
        if not other or other == "0":
            return None
        extension = single.get("extension") or {}
        if isinstance(extension, str):
            try:
                extension = json.loads(extension)
            except ValueError:
                extension = {}
        last = conv.get("lastMessage") or {}
        last_message = last.get("message") if isinstance(last, dict) else {}
        last_message = last_message if isinstance(last_message, dict) else {}
        last_custom = ((last_message.get("content") or {}).get("custom") or {})
        summary = str(last_custom.get("summary") or "")
        if not summary and last_custom.get("data"):
            decoded = load_content_json(last_custom["data"])
            summary, images, msg_type = interpret_content(decoded or {})
            if not summary and images:
                summary = "[图片]"
        last_ext = last_message.get("extension") or {}
        other_name = ""
        if isinstance(last_ext, dict) and _strip(last_ext.get("senderUserId")) == other:
            other_name = str(last_ext.get("reminderTitle") or "")
        return {
            "cid": cid, "rawCid": raw_cid, "otherUserId": other,
            "otherUserName": other_name, "otherUserAvatar": "",
            "itemTitle": str(extension.get("itemTitle") or "") if isinstance(extension, dict) else "",
            "lastMessageSummary": summary[:100], "lastMessageTime": int(conv.get("modifyTime") or 0),
            "unreadCount": int(conv.get("redPoint") or 0),
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _stored_message(item: ChatMessageRecord) -> dict:
    payload = item.payload if isinstance(item.payload, dict) else {}
    return {
        "messageId": str(item.message_id or item.id), "senderId": str(item.sender_id or ""),
        "senderName": str(item.sender_name or ""), "isSelf": bool(item.is_self),
        "type": item.msg_type, "text": item.text or "", "images": item.images or [],
        "time": int(item.message_time or 0),
        "itemId": str(payload.get("itemId") or payload.get("item_id") or ""),
        "orderId": str(payload.get("orderId") or payload.get("order_id") or ""),
    }


QUICK_PHRASE_FEATURE = "chat-quick-phrases"


def _phrase_statement(user: dict):
    return select(FeatureRecord).where(
        FeatureRecord.feature == QUICK_PHRASE_FEATURE,
        FeatureRecord.owner_id == _uid(user),
    )


def _serialize_phrase(item: FeatureRecord) -> dict[str, Any]:
    payload = dict(item.payload or {})
    return {
        "id": item.id,
        "title": str(payload.get("title") or ""),
        "content": str(payload.get("content") or ""),
        "sort_order": int(payload.get("sort_order") or 0),
    }


@router.get("/quick-phrases")
async def list_quick_phrases(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = list((await db.execute(
        _phrase_statement(user).order_by(FeatureRecord.id.asc())
    )).scalars().all())
    phrases = [_serialize_phrase(item) for item in rows]
    phrases.sort(key=lambda item: (item["sort_order"], item["id"]))
    return ok(phrases, "快捷短语查询成功")


@router.post("/quick-phrases")
async def create_quick_phrase(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    data = payload or {}
    title = str(data.get("title") or "").strip()
    content = str(data.get("content") or "").strip()
    if not title or not content:
        raise HTTPException(422, "快捷短语名称和内容不能为空")
    item = FeatureRecord(
        owner_id=_uid(user),
        feature=QUICK_PHRASE_FEATURE,
        external_id=secrets.token_hex(12),
        status="active",
        payload={"title": title, "content": content, "sort_order": int(data.get("sort_order") or 0)},
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return ok(_serialize_phrase(item), "快捷短语已添加")


@router.put("/quick-phrases/{phrase_id}")
async def update_quick_phrase(phrase_id: int, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = (await db.execute(_phrase_statement(user).where(FeatureRecord.id == phrase_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "快捷短语不存在")
    data = dict(item.payload or {})
    incoming = payload or {}
    if "title" in incoming:
        data["title"] = str(incoming.get("title") or "").strip()
    if "content" in incoming:
        data["content"] = str(incoming.get("content") or "").strip()
    if not data.get("title") or not data.get("content"):
        raise HTTPException(422, "快捷短语名称和内容不能为空")
    if "sort_order" in incoming:
        try:
            data["sort_order"] = int(incoming.get("sort_order") or 0)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, "排序值必须是数字") from exc
    item.payload = data
    await db.commit()
    await db.refresh(item)
    return ok(_serialize_phrase(item), "快捷短语已更新")


@router.delete("/quick-phrases/{phrase_id}")
async def delete_quick_phrase(phrase_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = (await db.execute(_phrase_statement(user).where(FeatureRecord.id == phrase_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "快捷短语不存在")
    await db.delete(item)
    await db.commit()
    return ok({"id": phrase_id, "deleted": True}, "快捷短语已删除")


async def _save_message(account_id: int, cid: str, message: dict, db: AsyncSession) -> tuple[ChatMessageRecord, bool]:
    message_id = str(message.get("messageId") or "")
    existing = None
    if message_id:
        existing = (await db.execute(select(ChatMessageRecord).where(ChatMessageRecord.account_id == account_id, ChatMessageRecord.message_id == message_id))).scalar_one_or_none()
    if existing:
        incoming = dict(message)
        current = dict(existing.payload or {}) if isinstance(existing.payload, dict) else {}
        merged = {**current, **incoming}
        if merged != current:
            existing.payload = merged
            if incoming.get("itemId") and not existing.text:
                existing.text = str(incoming.get("text") or "")
            await db.commit()
        return existing, False
    item = ChatMessageRecord(
        account_id=account_id, cid=cid, message_id=message_id or None,
        sender_id=str(message.get("senderId") or ""), sender_name=str(message.get("senderName") or ""),
        is_self=bool(message.get("isSelf")), msg_type=str(message.get("type") or "text"),
        text=str(message.get("text") or ""), images=message.get("images") or [],
        message_time=int(message.get("time") or 0), payload=message,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item, True


async def _reconcile_history_orders(account: Account, messages: list[dict], db: AsyncSession) -> None:
    """把历史聊天卡片里的订单号补回订单表，但不从历史消息触发发货。"""
    def valid_buyer_name(value: Any) -> str:
        name = str(value or "").strip()
        if not name or name.startswith(("[", "【")):
            return ""
        if any(marker in name for marker in ("拍下", "付款", "发货", "退款", "评价", "交易成功", "订单")):
            return ""
        return name[:255]

    buyer_names: dict[str, str] = {}
    for message in messages:
        sender_id = _strip(message.get("senderId") or message.get("sender_id"))
        name = valid_buyer_name(message.get("senderName") or message.get("sender_name"))
        if sender_id and not bool(message.get("isSelf")) and name:
            buyer_names.setdefault(sender_id, name)

    grouped: dict[str, dict[str, Any]] = {}
    unbound_messages: list[dict[str, Any]] = []
    for message in messages:
        order_no = str(message.get("orderId") or message.get("order_id") or "").strip()
        if not order_no:
            unbound_messages.append(message)
            continue
        bucket = grouped.setdefault(order_no, {"messages": [], "status": "unknown", "item_id": "", "buyer_id": "", "buyer_nick": ""})
        bucket["messages"].append(message)
        bucket["item_id"] = bucket["item_id"] or str(message.get("itemId") or message.get("item_id") or "").strip()
        if not bool(message.get("isSelf")) and str(message.get("senderId") or "").strip():
            sender_id = str(message.get("senderId") or "").strip()
            bucket["buyer_id"] = bucket["buyer_id"] or sender_id
            bucket["buyer_nick"] = bucket["buyer_nick"] or valid_buyer_name(message.get("senderName")) or buyer_names.get(sender_id, "")
        bucket["status"] = merge_order_status(bucket["status"], status_from_chat_text(message.get("text")))

    # 闲鱼的“你已发货”和“交易成功”卡片经常不携带订单号，但通常会
    # 保留商品 ID/买家 ID。只有能唯一对应到当前会话订单时才归属，
    # 避免多订单会话把状态误写到其他订单。
    for message in unbound_messages:
        status = status_from_chat_text(message.get("text"))
        if status == "unknown":
            continue
        item_id = str(message.get("itemId") or message.get("item_id") or "").strip()
        buyer_id = str(message.get("senderId") or message.get("sender_id") or "").strip()
        candidates = list(grouped.values())
        if item_id:
            candidates = [row for row in candidates if row["item_id"] == item_id]
        if buyer_id:
            buyer_candidates = [row for row in candidates if row["buyer_id"] == buyer_id]
            if buyer_candidates:
                candidates = buyer_candidates
        if len(candidates) == 1:
            candidates[0]["status"] = merge_order_status(candidates[0]["status"], status)

    changed = False
    for order_no, bucket in grouped.items():
        if bucket["status"] == "unknown":
            continue
        item_id = bucket["item_id"] or None
        item = None
        if item_id:
            item = (
                await db.execute(
                    select(AccountContent).where(
                        AccountContent.account_id == account.id,
                        AccountContent.content_type == "product",
                        AccountContent.external_id == item_id,
                    )
                )
            ).scalar_one_or_none()
        order = (await db.execute(select(Order).where(Order.order_no == order_no))).scalar_one_or_none()
        safe_event_ids = [str(item.get("messageId") or "") for item in bucket["messages"] if item.get("messageId")]
        if order is None:
            order = Order(
                account_id=account.id,
                order_no=order_no[:64],
                buyer_id=bucket["buyer_id"] or None,
                buyer_nick=bucket["buyer_nick"] or None,
                product_id=item.id if item else None,
                item_external_id=item_id,
                item_title=str(item.title or "")[:255] if item and item.title else None,
                quantity=1,
                amount=0.0,
                status=bucket["status"],
                payload={"history_reconciled": {"message_ids": safe_event_ids, "item_id": item_id, "status": bucket["status"]}},
            )
            db.add(order)
            changed = True
            continue
        if order.account_id != account.id:
            continue
        merged_status = merge_order_status(order.status, bucket["status"])
        if order.status != merged_status:
            order.status = merged_status
            changed = True
        if order.buyer_nick and not valid_buyer_name(order.buyer_nick):
            order.buyer_nick = None
            changed = True
        for attr, value in (("buyer_id", bucket["buyer_id"] or None), ("buyer_nick", bucket["buyer_nick"] or None), ("item_external_id", item_id), ("product_id", item.id if item else None), ("item_title", str(item.title or "")[:255] if item and item.title else None)):
            if getattr(order, attr) in (None, "") and value not in (None, ""):
                setattr(order, attr, value)
                changed = True
        payload = dict(order.payload or {}) if isinstance(order.payload, dict) else {}
        payload["history_reconciled"] = {"message_ids": safe_event_ids, "item_id": item_id, "status": bucket["status"]}
        if order.payload != payload:
            order.payload = payload
            changed = True
    if changed:
        await db.commit()


async def _broadcast(account_id: int, payload: dict) -> None:
    async with _browser_clients_lock:
        clients = list(_browser_clients.get(account_id, set()))
    failed: list[WebSocket] = []
    for client in clients:
        try:
            await client.send_json(payload)
        except Exception:
            failed.append(client)
    if failed:
        async with _browser_clients_lock:
            current = _browser_clients.get(account_id, set())
            for client in failed:
                current.discard(client)


async def _dispatch_message_notifications(account_id: int, message: dict) -> None:
    content = str(message.get("text") or "[图片]")
    try:
        async with async_session_maker() as db:
            rows = list((await db.execute(
                select(MessageNotificationBinding, NotificationChannel)
                .join(NotificationChannel, NotificationChannel.id == MessageNotificationBinding.channel_id)
                .where(MessageNotificationBinding.account_id == account_id, MessageNotificationBinding.enabled.is_(True), NotificationChannel.enabled.is_(True))
            )).all())
            for binding, channel in rows:
                record = Notification(channel=channel.channel_type, title="闲鱼新消息", content=content, status="pending")
                db.add(record)
                try:
                    await send_channel(channel, "闲鱼新消息", content)
                    record.status = "sent"
                except Exception as exc:
                    record.status = "failed"
                    record.content = f"{content}\n发送失败：{str(exc)[:200]}"
            await db.commit()
    except Exception:
        return


@router.get("/accounts")
async def list_chat_accounts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    statement = select(Account).order_by(Account.id.desc())
    statement = statement.where(Account.user_id == _uid(user))
    accounts = list((await db.execute(statement)).scalars().all())
    online_ids = await _online_ids()
    start = (page - 1) * page_size
    rows = accounts[start : start + page_size]
    names_changed = False
    for account in rows:
        nickname = extract_account_nickname(account.cookie)
        if nickname and is_generated_account_name(account.account_name, account.goofish_id):
            account.account_name = nickname
            names_changed = True
    if names_changed:
        await db.commit()
    return ok(
        {
            "items": [
                {
                    "account_id": str(account.id),
                    "display_name": display_account_name(account.account_name, account.goofish_id, account.cookie),
                    "remark": display_account_name(account.account_name, account.goofish_id, account.cookie),
                    "connected": str(account.id) in online_ids,
                    "status": account.status,
                }
                for account in rows
            ],
            "total": len(accounts),
            "hasMore": start + page_size < len(accounts),
        },
        "查询成功",
    )


@router.get("/account-profile/{account_id}")
async def get_account_profile(
    account_id: int,
    cid: str | None = Query(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """查询闲鱼卖家资料，并在账号名为数字时持久化真实昵称。"""
    account = await _get_account(account_id, user, db)
    nick = ""
    avatar = ""
    if cid and (account.cookie or "").strip():
        result = await mtop_call(
            api="mtop.taobao.idlemessage.pc.user.query",
            data={"type": 0, "sessionType": 1, "sessionId": str(cid), "isOwner": True},
            cookies_str=str(account.cookie),
            account_id=str(account.goofish_id or account.id),
            version="4.0",
            extra_params={"spm_cnt": "a21ybx.im.0.0"},
        )
        latest_cookie = str(result.get("cookies_str") or account.cookie)
        if latest_cookie != account.cookie:
            account.cookie = latest_cookie
            db.add(AccountCookie(account_id=account.id, cookie_value=latest_cookie, status="active"))
        response = result.get("res") if isinstance(result.get("res"), dict) else {}
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        user_info = data.get("userInfo") if isinstance(data.get("userInfo"), dict) else {}
        nick = str(user_info.get("nick") or user_info.get("fishNick") or "").strip()
        avatar = str(user_info.get("logo") or user_info.get("avatar") or "").strip()
        if nick and not nick.isdigit() and is_generated_account_name(account.account_name, account.goofish_id):
            account.account_name = nick[:64]
        await db.commit()
    return ok({
        "avatar": avatar,
        "nick": nick or display_account_name(account.account_name, account.goofish_id, account.cookie),
    }, "账号资料查询成功")


@router.post("/avatars/{account_id}")
async def query_user_infos(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """从已落库的真实聊天消息补填买家昵称，避免会话只显示数字 ID。"""
    await _get_account(account_id, user, db)
    queries = (payload or {}).get("queries") or []
    user_ids = {
        _strip(item.get("userId") or item.get("user_id"))
        for item in queries
        if isinstance(item, dict) and _strip(item.get("userId") or item.get("user_id"))
    }
    if not user_ids:
        return ok({}, "用户资料查询成功")
    rows = list((await db.execute(
        select(ChatMessageRecord)
        .where(ChatMessageRecord.account_id == account_id, ChatMessageRecord.sender_id.in_(user_ids))
        .order_by(ChatMessageRecord.id.desc())
        .limit(500)
    )).scalars().all())
    result = {user_id: {"avatar": "", "nick": ""} for user_id in user_ids}
    for row in rows:
        sender_id = _strip(row.sender_id)
        if sender_id in result and not result[sender_id]["nick"] and row.sender_name:
            result[sender_id]["nick"] = str(row.sender_name)
    return ok(result, "用户资料查询成功")


def _platform_chat_adapter_error(message: str) -> HTTPException:
    return HTTPException(status_code=501, detail=message)


@router.post("/send-image/{account_id}")
async def send_image_message(
    account_id: int,
    cid: str = Form(...),
    to_user_id: str = Form(..., alias="toUserId"),
    image: UploadFile = File(...),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """图片消息：保存到共享目录后由已连接的 IM 运行时上传 CDN 并发送。"""
    account = await _get_account(account_id, user, db)
    if not image.content_type or not image.content_type.lower().startswith("image/"):
        raise HTTPException(422, "请上传图片文件")
    content = await image.read()
    if not content:
        raise HTTPException(422, "上传文件为空")
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(422, "图片大小不能超过10MB")
    upload_dir = Path(settings.static_dir) / "uploads" / "chat"
    upload_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(image.filename or ".jpg").suffix.lower() or ".jpg"
    target = upload_dir / f"{uuid4().hex}{suffix}"
    try:
        target.write_bytes(content)
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                f"{settings.websocket_service_url.rstrip('/')}/internal/chat/{account.id}/send-image",
                json={"cid": cid, "to_user_id": to_user_id, "image_path": str(target)},
                headers={"X-Internal-Token": settings.jwt_secret},
            )
        try:
            result = response.json()
        except ValueError:
            result = {}
        if not response.is_success or not result.get("success"):
            detail = result.get("detail") or result.get("message") or "图片发送失败"
            raise HTTPException(502, str(detail))
        sent = result.get("data") or {}
        message = {
            "messageId": str(sent.get("messageId") or ""),
            "senderId": _strip(_parse_cookie(account.cookie or "").get("unb") or account.goofish_id or ""),
            "senderName": account.account_name,
            "isSelf": True,
            "type": "image",
            "text": "",
            "images": [str(sent.get("imageUrl") or "")],
            "time": int(time.time() * 1000),
        }
        await _save_message(account.id, _strip(cid), message, db)
        await _broadcast(account.id, {"event": "new_message", "account_id": str(account.id), "cid": _strip(cid), "message": message})
        return ok({"messageId": message["messageId"], "imageUrl": message["images"][0]}, "图片发送成功")
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, f"连接服务不可用：{str(exc)[:300]}") from exc
    finally:
        target.unlink(missing_ok=True)


@router.post("/recall-message/{account_id}")
async def recall_message(account_id: int, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    await _get_account(account_id, user, db)
    data = payload or {}
    message_id = str(data.get("messageId") or data.get("message_id") or "").strip()
    if not message_id:
        raise HTTPException(422, "缺少消息ID，无法撤回")
    try:
        message_time = int(data.get("messageTime") or data.get("message_time") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "消息时间无效") from exc
    if message_time and message_time < 1_000_000_000_000:
        message_time *= 1000
    if message_time and (int(time.time() * 1000) - message_time > 120_000 or message_time - int(time.time() * 1000) > 10_000):
        raise HTTPException(409, "消息发送超过两分钟，无法撤回")
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(
                f"{settings.websocket_service_url.rstrip('/')}/internal/chat/{account_id}/recall-message",
                json={"message_id": message_id},
                headers={"X-Internal-Token": settings.jwt_secret},
            )
        result = response.json()
        if not response.is_success or not result.get("success"):
            raise HTTPException(502, result.get("detail") or result.get("message") or "撤回失败")
        return ok({"messageId": message_id}, "消息已撤回")
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, f"连接服务不可用：{str(exc)[:300]}") from exc


@router.get("/official-blacklist/{account_id}/{cid}")
async def get_official_blacklist(account_id: int, cid: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    account = await _get_account(account_id, user, db)
    try:
        result = await official_blacklist(cookies=account.cookie or "", account_id=str(account.goofish_id or account.id), session_id=_strip(cid), action="query")
        return ok({"blocked": bool((result.get("data") or {}).get("isInBlack"))}, "官方黑名单状态查询成功")
    except XianyuPlatformError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/official-blacklist/{account_id}/{cid}/{action}")
async def change_official_blacklist(account_id: int, cid: str, action: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    account = await _get_account(account_id, user, db)
    if action not in {"add", "remove"}:
        raise HTTPException(422, "无效操作")
    try:
        result = await official_blacklist(cookies=account.cookie or "", account_id=str(account.goofish_id or account.id), session_id=_strip(cid), action=action)
        blocked = action == "add"
        return ok({"blocked": blocked}, "已加入闲鱼官方黑名单" if blocked else "已解除闲鱼官方黑名单")
    except XianyuPlatformError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/conversations/{account_id}")
async def get_conversations(
    account_id: int,
    cursor: int | None = None,
    limit: int = Query(20, ge=1, le=100),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    account = await _get_account(account_id, user, db)
    live = await _runtime_query(account_id, "conversations", {"cursor": cursor, "limit": limit})
    myid = _strip((_parse_cookie(account.cookie or "").get("unb") or _parse_cookie(account.cookie or "").get("munb") or account.goofish_id or ""))
    conversations = []
    if live:
        for raw in live.get("userConvs", []) if isinstance(live.get("userConvs"), list) else []:
            if isinstance(raw, dict):
                parsed = _conversation_from_live(raw, myid)
                if parsed:
                    conversations.append(parsed)
    if not conversations:
        stored = list((await db.execute(
            select(ChatMessageRecord).where(ChatMessageRecord.account_id == account_id).order_by(ChatMessageRecord.message_time.desc(), ChatMessageRecord.id.desc()).limit(limit * 5)
        )).scalars().all())
        seen: set[str] = set()
        for item in stored:
            if item.cid in seen:
                continue
            seen.add(item.cid)
            conversations.append({
                "cid": item.cid, "rawCid": f"{item.cid}@goofish", "otherUserId": str(item.sender_id or ""),
                "otherUserName": str(item.sender_name or ""), "otherUserAvatar": "", "itemTitle": "",
                "lastMessageSummary": item.text or "[图片]", "lastMessageTime": int(item.message_time or 0),
                "unreadCount": 0,
            })
    return ok({"conversations": conversations[:limit], "hasMore": False, "nextCursor": None}, "查询成功")


@router.get("/messages/{account_id}/{cid}")
async def get_messages(
    account_id: int,
    cid: str,
    cursor: int | None = None,
    limit: int = Query(20, ge=1, le=100),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    account = await _get_account(account_id, user, db)
    live = await _runtime_query(account_id, "messages", {"cid": cid, "cursor": cursor, "limit": limit})
    values: list[dict] = []
    myid = _strip((_parse_cookie(account.cookie or "").get("unb") or _parse_cookie(account.cookie or "").get("munb") or account.goofish_id or ""))
    if live:
        models = live.get("userMessageModels", [])
        if isinstance(models, list):
            for model in models:
                if isinstance(model, dict):
                    parsed = _message_from_history(model, myid)
                    if parsed:
                        values.append(parsed)
                        await _save_message(account_id, _strip(cid), parsed, db)
        values.reverse()
    if not values:
        statement = select(ChatMessageRecord).where(ChatMessageRecord.account_id == account_id, ChatMessageRecord.cid == _strip(cid)).order_by(ChatMessageRecord.message_time.desc(), ChatMessageRecord.id.desc()).limit(limit)
        stored = list((await db.execute(statement)).scalars().all())
        values = [_stored_message(item) for item in reversed(stored)]
    await _reconcile_history_orders(account, values, db)
    return ok({"messages": values, "hasMore": bool(live.get("hasMore")) if live else False, "nextCursor": live.get("nextCursor") if live else None}, "查询成功")


@router.get("/customer-orders/{account_id}/{buyer_id}")
async def get_customer_orders(
    account_id: int,
    buyer_id: str,
    chat_id: str | None = Query(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """按当前会话买家读取真实本地订单，供聊天工作台订单面板使用。"""
    account = await _get_account(account_id, user, db)
    if chat_id:
        stored_rows = list((await db.execute(
            select(ChatMessageRecord)
            .where(ChatMessageRecord.account_id == account.id, ChatMessageRecord.cid == _strip(chat_id))
            .order_by(ChatMessageRecord.message_time.asc(), ChatMessageRecord.id.asc())
            .limit(200)
        )).scalars().all())
        await _reconcile_history_orders(account, [_stored_message(row) for row in stored_rows], db)
    clean_buyer_id = _strip(buyer_id)
    rows = (
        await db.execute(
            select(Order)
            .where(Order.account_id == account.id, Order.buyer_id == clean_buyer_id)
            .order_by(Order.id.desc())
            .limit(50)
        )
    ).scalars().all()
    return ok([
        {
            "order_no": order.order_no,
            "item_id": str(order.item_external_id or order.product_id or "-"),
            "item_title": order.item_title or "未命名商品",
            "buyer_id": order.buyer_id or clean_buyer_id,
            "quantity": int(order.quantity or 1),
            "amount": str(order.amount or "0"),
            "status": order.status,
            "delivery_method": order.delivery_method or "",
            "delivery_fail_reason": order.delivery_fail_reason or "",
            "card_only_delivered": bool(order.card_only_delivered),
            "placed_at": order.created_at.isoformat() if order.created_at else "",
        }
        for order in rows
    ], "客户订单查询成功")


@router.post("/send-message/{account_id}")
async def send_message(account_id: int, payload: dict[str, Any], user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    account = await _get_account(account_id, user, db)
    cid, to_user_id, text = str(payload.get("cid") or ""), str(payload.get("toUserId") or payload.get("to_user_id") or ""), str(payload.get("text") or "")
    if not cid or not to_user_id or not text.strip():
        raise HTTPException(422, "会话、接收人和消息内容不能为空")
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(f"{settings.websocket_service_url.rstrip('/')}/internal/chat/{account_id}/send-text", json={"cid": cid, "to_user_id": to_user_id, "text": text.strip()}, headers={"X-Internal-Token": settings.jwt_secret})
        if not response.is_success:
            detail = response.json().get("detail", "发送失败")
            raise HTTPException(502, detail)
        result = response.json().get("data") or {}
    except httpx.HTTPError as exc:
        raise HTTPException(503, "连接服务不可用") from exc
    message_id = str(result.get("messageId") or result.get("message_id") or result.get("mid") or "").strip()
    if not message_id:
        message_id = f"local-{int(time.time() * 1000)}-{uuid4().hex[:12]}"
    sender_id = _strip(_parse_cookie(account.cookie or "").get("unb") or _parse_cookie(account.cookie or "").get("munb") or account.goofish_id or account.id)
    message = {
        "messageId": message_id,
        "senderId": sender_id,
        "senderName": display_account_name(account.account_name, account.goofish_id, account.cookie),
        "isSelf": True,
        "type": "text",
        "text": text.strip(),
        "images": [],
        "time": int(time.time() * 1000),
    }
    await _save_message(account.id, _strip(cid), message, db)
    await _broadcast(account.id, {"event": "new_message", "account_id": str(account.id), "cid": _strip(cid), "message": message})
    return ok({"messageId": message_id, "message": message}, "发送成功")


@router.post("/internal/events")
async def receive_internal_event(payload: dict[str, Any], x_internal_token: str | None = Header(default=None)):
    if not x_internal_token or not secrets.compare_digest(x_internal_token, settings.jwt_secret):
        raise HTTPException(401, "内部接口鉴权失败")
    try:
        account_id = int(payload.get("account_id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "账号ID无效") from exc
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    cid = _strip(event.get("cid"))
    if not cid or not message:
        return ok({"stored": False, "broadcast": False}, "非聊天推送已忽略")
    async with async_session_maker() as db:
        _, created = await _save_message(account_id, cid, message, db)
        status_updated = await apply_live_order_status(db, account_id, message)
    browser_payload = {"event": "new_message", "account_id": str(account_id), "cid": cid, "message": message}
    await _broadcast(account_id, browser_payload)
    if created and not bool(message.get("isSelf")):
        asyncio.create_task(_dispatch_message_notifications(account_id, message))
        # 付款提醒本身就是最及时的订单事件。部分卖家账号无法访问
        # merchant.sold.get，不能只依赖订单列表同步，否则订单已拍下但
        # 本地没有订单记录，卡密自动发货永远不会触发。
        asyncio.create_task(auto_deliver_live_event(account_id, cid, message))
        # 自动回复必须在实时事件入口触发。历史消息接口只负责展示，不能在
        # 用户打开聊天页或刷新列表时重复向买家发送消息。
        asyncio.create_task(dispatch_auto_reply(account_id, cid, message))
    return ok({"stored": created, "broadcast": True, "order_status_updated": status_updated}, "实时消息已接收")


@router.post("/connect/{account_id}")
async def connect_account(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    account = await _get_account(account_id, user, db)
    if account.status != "active":
        raise HTTPException(status_code=409, detail="账号未启用，无法连接")
    runtime = await _runtime_call(account, "start")
    return ok({"account_id": str(account.id), "connected": bool(runtime.get("is_connected")), "runtime": runtime}, "账号连接任务已启动")


@router.post("/disconnect/{account_id}")
async def disconnect_account(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    account = await _get_account(account_id, user, db)
    runtime = await _runtime_call(account, "stop")
    return ok({"account_id": str(account.id), "connected": False, "runtime": runtime}, "账号已断开连接")


@router.websocket("/ws/{account_id}")
async def chat_websocket(socket: WebSocket, account_id: int):
    """浏览器聊天通道的鉴权与保活握手。"""
    token = socket.query_params.get("token") or ""
    user = decode_access_token(token)
    if not user:
        await socket.close(code=1008, reason="登录状态已失效")
        return
    async with async_session_maker() as db:
        statement = select(Account).where(Account.id == account_id, Account.user_id == _uid(user))
        account = (await db.execute(statement)).scalar_one_or_none()
    if account is None:
        await socket.close(code=1008, reason="账号不存在或无权访问")
        return

    await socket.accept()
    await socket.send_json({"event": "connected", "account_id": str(account_id)})
    async with _browser_clients_lock:
        _browser_clients.setdefault(account_id, set()).add(socket)
    try:
        while True:
            message = await socket.receive_json()
            if isinstance(message, dict) and (
                message.get("event") in {"ping", "heartbeat"}
                or message.get("type") in {"ping", "heartbeat"}
            ):
                await socket.send_json({"event": "pong", "account_id": str(account_id)})
    except WebSocketDisconnect:
        return
    except Exception:
        await socket.close(code=1011)
    finally:
        async with _browser_clients_lock:
            clients = _browser_clients.get(account_id)
            if clients is not None:
                clients.discard(socket)
                if not clients:
                    _browser_clients.pop(account_id, None)
