"""通知渠道和账号消息通知的真实持久化接口。"""
from __future__ import annotations

import asyncio
import json
import smtplib
from email.mime.text import MIMEText
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models import Account, MessageNotificationBinding, NotificationChannel

router = APIRouter(prefix="/api/v1", tags=["通知配置"])


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _channel_data(item: NotificationChannel) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "type": item.channel_type,
        "channel_type": item.channel_type,
        "config": item.config or {},
        "enabled": bool(item.enabled),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


async def _channel(channel_id: int, user: dict[str, Any], db: AsyncSession) -> NotificationChannel:
    statement = select(NotificationChannel).where(NotificationChannel.id == channel_id)
    if not _is_admin(user):
        statement = statement.where(NotificationChannel.owner_id == _uid(user))
    item = (await db.execute(statement)).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "通知渠道不存在或无权访问")
    return item


@router.get("/notification-channels")
@router.get("/notification-channels/")
async def list_notification_channels(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    statement = select(NotificationChannel).order_by(NotificationChannel.id.desc())
    if not _is_admin(user):
        statement = statement.where(NotificationChannel.owner_id == _uid(user))
    rows = list((await db.execute(statement)).scalars().all())
    return ok([_channel_data(item) for item in rows], "查询成功")


@router.post("/notification-channels")
@router.post("/notification-channels/")
async def create_notification_channel(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    name = str(payload.get("name") or "").strip()
    channel_type = str(payload.get("type") or payload.get("channel_type") or "").strip().lower()
    if not name or not channel_type:
        raise HTTPException(422, "渠道名称和类型不能为空")
    config = payload.get("config") or payload.get("channel_config") or {}
    if isinstance(config, str):
        try:
            config = json.loads(config) if config.strip() else {}
        except ValueError as exc:
            raise HTTPException(422, "渠道配置必须是有效JSON") from exc
    if not isinstance(config, dict):
        raise HTTPException(422, "渠道配置格式不正确")
    item = NotificationChannel(owner_id=_uid(user), name=name, channel_type=channel_type, config=config, enabled=bool(payload.get("enabled", True)))
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return ok(_channel_data(item), "渠道已创建")


@router.put("/notification-channels/{channel_id}")
async def update_notification_channel(channel_id: int, payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = await _channel(channel_id, user, db)
    if "name" in payload and str(payload["name"]).strip():
        item.name = str(payload["name"]).strip()
    if "enabled" in payload:
        item.enabled = bool(payload["enabled"])
    if "config" in payload:
        config = payload["config"]
        if isinstance(config, str):
            try:
                config = json.loads(config) if config.strip() else {}
            except ValueError as exc:
                raise HTTPException(422, "渠道配置必须是有效JSON") from exc
        if not isinstance(config, dict):
            raise HTTPException(422, "渠道配置格式不正确")
        item.config = config
    await db.commit()
    await db.refresh(item)
    return ok(_channel_data(item), "渠道已更新")


@router.delete("/notification-channels/{channel_id}")
async def delete_notification_channel(channel_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = await _channel(channel_id, user, db)
    await db.execute(delete(MessageNotificationBinding).where(MessageNotificationBinding.channel_id == item.id))
    await db.delete(item)
    await db.commit()
    return ok({"id": channel_id, "deleted": True}, "渠道已删除")


def _placeholder(value: Any) -> bool:
    text = str(value or "")
    return not text or any(mark in text for mark in ("你的", "xxx", "...", "example.com"))


async def send_channel(channel: NotificationChannel, title: str, content: str) -> None:
    """按渠道类型发送一条消息；配置无效时抛出可读错误。"""
    config = channel.config or {}
    channel_type = channel.channel_type.lower()
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        if channel_type == "dingtalk":
            url = config.get("webhook_url")
            if _placeholder(url):
                raise RuntimeError("钉钉Webhook尚未配置")
            response = await client.post(url, json={"msgtype": "text", "text": {"content": f"{title}\n{content}"}})
        elif channel_type == "feishu":
            url = config.get("webhook_url")
            if _placeholder(url):
                raise RuntimeError("飞书Webhook尚未配置")
            response = await client.post(url, json={"msg_type": "text", "content": {"text": f"{title}\n{content}"}})
        elif channel_type == "wechat":
            url = config.get("webhook_url")
            if _placeholder(url):
                raise RuntimeError("企业微信Webhook尚未配置")
            response = await client.post(url, json={"msgtype": "text", "text": {"content": f"{title}\n{content}"}})
        elif channel_type == "webhook":
            url = config.get("webhook_url")
            if _placeholder(url):
                raise RuntimeError("Webhook地址尚未配置")
            headers = config.get("headers") if isinstance(config.get("headers"), dict) else {}
            method = str(config.get("method") or "POST").upper()
            response = await client.request(method, url, json={"title": title, "content": content}, headers=headers)
        elif channel_type == "bark":
            key = config.get("device_key")
            server = str(config.get("server_url") or "https://api.day.app").rstrip("/")
            if _placeholder(key):
                raise RuntimeError("Bark设备密钥尚未配置")
            response = await client.get(f"{server}/{key}/{title}", params={"body": content})
        elif channel_type == "telegram":
            token, chat_id = config.get("bot_token"), config.get("chat_id")
            if _placeholder(token) or _placeholder(chat_id):
                raise RuntimeError("Telegram Bot Token或Chat ID尚未配置")
            response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": f"{title}\n{content}"})
        elif channel_type == "pushplus":
            token = config.get("token")
            if _placeholder(token):
                raise RuntimeError("PushPlus Token尚未配置")
            response = await client.post("https://www.pushplus.plus/send", json={"token": token, "title": title, "content": content, "topic": config.get("topic", ""), "template": config.get("template", "txt")})
        elif channel_type == "email":
            await asyncio.to_thread(_send_email, config, title, content)
            return
        else:
            raise RuntimeError(f"不支持的通知渠道类型：{channel_type}")
        response.raise_for_status()
        try:
            result = response.json()
        except ValueError:
            result = {}
        if isinstance(result, dict) and result.get("errcode") not in (None, 0, "0"):
            raise RuntimeError(str(result.get("errmsg") or result.get("message") or "第三方渠道返回失败"))


def _send_email(config: dict[str, Any], title: str, content: str) -> None:
    server = str(config.get("smtp_server") or "")
    user = str(config.get("email_user") or "")
    password = str(config.get("email_password") or "")
    recipient = str(config.get("recipient_email") or "")
    if _placeholder(server) or _placeholder(user) or _placeholder(password) or _placeholder(recipient):
        raise RuntimeError("邮件SMTP配置不完整")
    port = int(config.get("smtp_port") or 587)
    message = MIMEText(content, "plain", "utf-8")
    message["Subject"], message["From"], message["To"] = title, user, recipient
    with smtplib.SMTP(server, port, timeout=15) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(message)


@router.post("/notification-channels/{channel_id}/test")
async def test_notification_channel(channel_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = await _channel(channel_id, user, db)
    try:
        await send_channel(item, "系统通知测试", "通知渠道连接测试成功。")
    except Exception as exc:
        raise HTTPException(502, f"测试发送失败：{str(exc)[:300]}") from exc
    return ok({"id": channel_id, "sent": True}, "测试消息发送成功")


async def _account(account_id: int, user: dict[str, Any], db: AsyncSession) -> Account:
    statement = select(Account).where(Account.id == account_id)
    if not _is_admin(user):
        statement = statement.where(Account.user_id == _uid(user))
    item = (await db.execute(statement)).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "账号不存在或无权访问")
    return item


async def _binding_rows(user: dict[str, Any], db: AsyncSession, account_id: int | None = None):
    statement = select(MessageNotificationBinding, NotificationChannel).join(NotificationChannel, NotificationChannel.id == MessageNotificationBinding.channel_id).order_by(MessageNotificationBinding.id.desc())
    if account_id is not None:
        statement = statement.where(MessageNotificationBinding.account_id == account_id)
    if not _is_admin(user):
        statement = statement.where(MessageNotificationBinding.owner_id == _uid(user))
    return list((await db.execute(statement)).all())


def _binding_data(binding: MessageNotificationBinding, channel: NotificationChannel) -> dict[str, Any]:
    return {"id": binding.id, "cookie_id": str(binding.account_id), "account_id": binding.account_id, "channel_id": binding.channel_id, "channel_name": channel.name, "channel_type": channel.channel_type, "enabled": bool(binding.enabled)}


@router.get("/message-notifications")
@router.get("/message-notifications/")
async def list_message_notifications(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = await _binding_rows(user, db)
    return ok({"items": [_binding_data(binding, channel) for binding, channel in rows], "total": len(rows)})


@router.get("/message-notifications/{account_id}")
async def list_account_message_notifications(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    await _account(account_id, user, db)
    rows = await _binding_rows(user, db, account_id)
    return ok([_binding_data(binding, channel) for binding, channel in rows])


@router.post("/message-notifications/{account_id}")
async def set_message_notification(account_id: int, payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    account = await _account(account_id, user, db)
    channel_id = int(payload.get("channel_id") or 0)
    channel = await _channel(channel_id, user, db)
    owner_id = account.user_id
    statement = select(MessageNotificationBinding).where(MessageNotificationBinding.owner_id == owner_id, MessageNotificationBinding.account_id == account_id, MessageNotificationBinding.channel_id == channel_id)
    binding = (await db.execute(statement)).scalar_one_or_none()
    if binding is None:
        binding = MessageNotificationBinding(owner_id=owner_id, account_id=account_id, channel_id=channel_id, enabled=bool(payload.get("enabled", True)))
        db.add(binding)
    else:
        binding.enabled = bool(payload.get("enabled", True))
    await db.commit()
    await db.refresh(binding)
    return ok(_binding_data(binding, channel), "消息通知已保存")


@router.put("/message-notifications/{binding_id}")
async def update_message_notification(binding_id: int, payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    statement = select(MessageNotificationBinding).where(MessageNotificationBinding.id == binding_id)
    if not _is_admin(user):
        statement = statement.where(MessageNotificationBinding.owner_id == _uid(user))
    binding = (await db.execute(statement)).scalar_one_or_none()
    if binding is None:
        raise HTTPException(404, "消息通知不存在")
    binding.enabled = bool(payload.get("enabled", binding.enabled))
    await db.commit()
    channel = (await db.execute(select(NotificationChannel).where(NotificationChannel.id == binding.channel_id))).scalar_one()
    return ok(_binding_data(binding, channel), "消息通知已更新")


@router.delete("/message-notifications/{binding_id}")
async def delete_message_notification(binding_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    statement = select(MessageNotificationBinding).where(MessageNotificationBinding.id == binding_id)
    if not _is_admin(user):
        statement = statement.where(MessageNotificationBinding.owner_id == _uid(user))
    item = (await db.execute(statement)).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "消息通知不存在")
    await db.delete(item)
    await db.commit()
    return ok({"id": binding_id, "deleted": True}, "消息通知已删除")


@router.delete("/message-notifications/account/{account_id}")
async def delete_account_message_notifications(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    account = await _account(account_id, user, db)
    statement = delete(MessageNotificationBinding).where(MessageNotificationBinding.account_id == account_id)
    if not _is_admin(user):
        statement = statement.where(MessageNotificationBinding.owner_id == _uid(user))
    result = await db.execute(statement)
    await db.commit()
    return ok({"account_id": account.id, "deleted": int(result.rowcount or 0)}, "账号通知已清理")
