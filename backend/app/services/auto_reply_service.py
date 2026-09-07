"""实时聊天自动回复服务。

关键词配置和实时 IM 连接分别由后端、WebSocket 服务维护。本模块负责把两者
接起来：收到买家消息后，从当前账号的结构化关键词规则中匹配回复，并通过
WebSocket 内部发送接口发回闲鱼。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from uuid import uuid4
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select

from common.config import settings
from common.db.session import async_session_maker
from common.models import Account, FeatureRecord, User
from common.services.ai_provider_service import (
    generate_ai_reply,
    get_ai_settings_missing_fields,
    provider_name,
    read_ai_enabled,
)
from backend.app.services.account_settings import load_account_settings, load_platform_ai_settings
from backend.app.services.entitlements import FEATURE_AI_SMART_REPLY, get_effective_entitlement

logger = logging.getLogger("xr.backend.auto_reply")

KEYWORD_FEATURE = "keywords-with-item-id"
FILTER_FEATURE = "message-filters"

# 这些内容是闲鱼系统提示，不应触发普通关键词自动回复。
SYSTEM_MESSAGE_EXACT = {
    "[我已拍下，待付款]",
    "[你关闭了订单，钱款已原路退返]",
    "AI正在帮你回复消息，不错过每笔订单",
    "发来一条消息",
    "发来一条新消息",
    "卖家人不错？送Ta闲鱼小红花",
    "你人真不错，送你闲鱼小红花",
    "[你已确认收货，交易成功]",
    "[买家确认收货，交易成功]",
    "买家已确认收货，交易成功",
    "[你已发货]",
    "已发货",
    "[注意！小心假客服骗钱！]",
    "订单已签收",
    "[我完成了评价]",
    "我完成了评价",
    "[退款成功，钱款已原路退返]",
    "[买家申请退款]",
    "[卖家同意退款]",
    "温馨提醒：商品信息近期有过变更",
    "查看商品详情",
}
SYSTEM_MESSAGE_PARTS = (
    "[安全提醒]",
    "当前聊天内容可能包含诈骗信息",
)
AUTO_DELIVERY_PARTS = (
    "[我已付款，等待你发货]",
    "[已付款，待发货]",
    "我已付款，等待你发货",
    "[记得及时发货]",
)


@dataclass(frozen=True)
class AutoReplyMatch:
    """一次关键词命中的结果。"""

    reply: str
    keyword: str
    rule_id: int
    item_id: str
    rule_type: str
    response_type: str = "text"
    image_url: str = ""


def split_keyword_lines(value: Any) -> list[str]:
    """把一条规则中的多行关键词拆开，并忽略空行。"""
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _normalized(value: Any) -> str:
    """Normalize text before matching while keeping substring semantics.

    The original system uses case-insensitive ``keyword in message`` matching.
    NFKC plus removal of whitespace/zero-width formatting characters preserves
    that behavior and also handles messages such as ``你 好`` or full-width
    punctuation without turning matching into an unsafe semantic guess.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        char
        for char in text
        if not char.isspace() and unicodedata.category(char) != "Cf"
    )


def _is_system_message(text: str) -> bool:
    normalized = text.strip()
    return normalized in SYSTEM_MESSAGE_EXACT or any(part in normalized for part in SYSTEM_MESSAGE_PARTS)


def _is_auto_delivery_message(text: str) -> bool:
    return any(part in text for part in AUTO_DELIVERY_PARTS)


def _format_reply(reply: str, message: dict[str, Any], item_id: str) -> str:
    """兼容旧版回复变量；变量不完整时保留原始回复，不吞掉整条规则。"""
    values = {
        "send_user_name": str(message.get("senderName") or ""),
        "send_user_id": str(message.get("senderId") or ""),
        "send_message": str(message.get("text") or ""),
        "item_id": item_id,
    }
    try:
        return reply.format(**values).strip()
    except (KeyError, IndexError, ValueError):
        logger.warning("自动回复变量替换失败，已使用原始回复内容")
        return reply.strip()


def _message_item_id(message: dict[str, Any]) -> str:
    for key in ("itemId", "item_id", "goodsId", "goods_id"):
        value = message.get(key)
        if value:
            return str(value).strip()
    return ""


async def _load_account(account_id: int) -> Account | None:
    async with async_session_maker() as db:
        return (
            await db.execute(select(Account).where(Account.id == account_id))
        ).scalar_one_or_none()


async def _load_rules(account: Account) -> list[FeatureRecord]:
    async with async_session_maker() as db:
        rows = (
            await db.execute(
                select(FeatureRecord)
                .where(
                    FeatureRecord.owner_id == int(account.user_id),
                    FeatureRecord.feature == KEYWORD_FEATURE,
                    FeatureRecord.status == "active",
                )
                .order_by(FeatureRecord.id.asc())
            )
        ).scalars().all()
        # 只返回当前账号的规则。账号 ID 保存在 payload 中，是现行关键词接口
        # 的持久化格式；不能只按 owner_id 查询，否则会串号回复。
        return [
            row
            for row in rows
            if str((row.payload or {}).get("account_id") or "") == str(account.id)
        ]


async def _should_skip_reply(account: Account, text: str) -> bool:
    """读取“跳过自动回复”过滤词，保持过滤规则按账号生效。"""
    async with async_session_maker() as db:
        rows = (
            await db.execute(
                select(FeatureRecord)
                .where(
                    FeatureRecord.owner_id == int(account.user_id),
                    FeatureRecord.feature == FILTER_FEATURE,
                    FeatureRecord.status == "active",
                )
            )
        ).scalars().all()
    message = _normalized(text)
    for row in rows:
        payload = row.payload or {}
        if str(payload.get("account_id") or "") != str(account.id):
            continue
        if str(payload.get("filter_type") or "") != "skip_reply":
            continue
        if _normalized(payload.get("keyword")) and _normalized(payload.get("keyword")) in message:
            return True
    return False


async def match_keyword_reply(account: Account, message: dict[str, Any]) -> AutoReplyMatch | None:
    """按“商品关键词优先、通用关键词其次”的顺序匹配一条文本规则。"""
    text = str(message.get("text") or "").strip()
    if not text or str(message.get("type") or "text").lower() == "image":
        return None
    if _is_system_message(text) or _is_auto_delivery_message(text):
        return None
    if await _should_skip_reply(account, text):
        return None

    item_id = _message_item_id(message)
    rules = await _load_rules(account)

    # 与旧版一致：有商品 ID 时先检查商品专属规则；没有商品 ID 的规则
    # 仍然作为通用规则处理。图片规则通过同一条 IM scope 发送图片消息。
    candidates: list[tuple[FeatureRecord, bool]] = []
    for row in rules:
        payload = row.payload or {}
        rule_item_id = str(payload.get("item_id") or "").strip()
        if rule_item_id:
            if item_id and rule_item_id == item_id:
                candidates.append((row, True))
        else:
            candidates.append((row, False))

    candidates.sort(key=lambda pair: (not pair[1], pair[0].id))
    normalized_text = _normalized(text)
    for row, is_item_rule in candidates:
        payload = row.payload or {}
        for keyword in split_keyword_lines(payload.get("keyword")):
            if _normalized(keyword) not in normalized_text:
                continue
            response_type = str(payload.get("type") or "text").lower()
            if response_type == "image":
                image_url = str(payload.get("image_url") or payload.get("imageUrl") or "").strip()
                if not image_url:
                    logger.warning("关键词规则 %s 图片地址为空，跳过回复", row.id)
                    continue
                description = _format_reply(str(payload.get("description") or ""), message, item_id)
                return AutoReplyMatch(reply=description, keyword=keyword, rule_id=int(row.id), item_id=str(payload.get("item_id") or ""), rule_type="keyword_item" if is_item_rule else "keyword_common", response_type="image", image_url=image_url)
            raw_reply = str(payload.get("reply") or "")
            reply = _format_reply(raw_reply, message, item_id)
            return AutoReplyMatch(
                reply=reply,
                keyword=keyword,
                rule_id=int(row.id),
                item_id=str(payload.get("item_id") or ""),
                rule_type="keyword_item" if is_item_rule else "keyword_common",
            )
    return None


def _time_in_range(start_value: Any, end_value: Any) -> bool:
    """判断当前北京时间是否落在 AI 配置的启用时间段内。"""
    start = str(start_value or "").strip()
    end = str(end_value or "").strip()
    if not start or not end:
        return True
    try:
        def parse(value: str):
            parts = [int(item) for item in value.split(":")[:2]]
            return parts[0] * 60 + parts[1]

        start_minutes = parse(start)
        end_minutes = parse(end)
        now = datetime.now(timezone(timedelta(hours=8)))
        current_minutes = now.hour * 60 + now.minute
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes <= end_minutes
        return current_minutes >= start_minutes or current_minutes <= end_minutes
    except (TypeError, ValueError, IndexError):
        logger.warning("账号 %s AI时间范围格式无效，按全天处理", account.id)
        return True


async def _match_ai_or_default_reply(account: Account, message: dict[str, Any]) -> AutoReplyMatch | None:
    """关键词未命中时按 AI > 账号默认回复的顺序生成回复。"""
    async with async_session_maker() as db:
        account_settings = await load_account_settings(db, int(account.user_id), int(account.id))
        platform_ai_settings = await load_platform_ai_settings(db, int(account.user_id))
        owner = await db.get(User, int(account.user_id))
        principal = {
            "sub": str(account.user_id),
            "role": owner.role if owner is not None else "user",
            "plan_code": owner.plan_code if owner is not None else "NORMAL",
        }
        try:
            ai_entitlement = await get_effective_entitlement(db, principal, FEATURE_AI_SMART_REPLY)
            ai_allowed = ai_entitlement.enabled
        except Exception as exc:  # fail closed when entitlement storage is unavailable
            ai_allowed = False
            logger.warning("账号 %s AI授权读取失败，按禁用处理：%s", account.id, str(exc)[:300])

    item_id = _message_item_id(message)
    text = str(message.get("text") or "").strip()
    # AI 配置属于平台账号；关键词、过滤词和默认回复仍然属于当前闲鱼账号。
    ai = dict(platform_ai_settings.get("ai_settings") or {})
    if platform_ai_settings.get("ai_enabled"):
        ai["ai_enabled"] = True
    else:
        ai.setdefault("ai_enabled", False)
    if ai_allowed and read_ai_enabled(ai) and _time_in_range(ai.get("ai_time_range_start"), ai.get("ai_time_range_end")):
        missing = get_ai_settings_missing_fields(ai)
        if not missing:
            custom_prompt = str(ai.get("custom_prompts") or "").strip()
            system_prompt = (
                "你是闲鱼卖家客服。只输出给买家的最终回复，不要解释、不要前缀，"
                "语气友好简洁，控制在40字以内。不得编造商品信息、承诺无法确认的事项。"
            )
            if custom_prompt:
                system_prompt += f"\n卖家补充要求：{custom_prompt[:1000]}"
            if item_id:
                system_prompt += f"\n当前商品ID：{item_id}"
            try:
                reply = await generate_ai_reply(
                    ai.get("provider_type"),
                    ai.get("base_url"),
                    ai.get("api_key"),
                    ai.get("model_name"),
                    [{"role": "system", "content": system_prompt}, {"role": "user", "content": text}],
                )
                if reply:
                    return AutoReplyMatch(
                        reply=reply,
                        keyword="AI",
                        rule_id=0,
                        item_id=item_id,
                        rule_type="ai",
                    )
            except Exception as exc:
                logger.warning("账号 %s AI自动回复失败，将尝试默认回复：%s", account.id, str(exc)[:400])
        else:
            logger.warning("账号 %s AI已开启但配置不完整：%s", account.id, "、".join(missing))

    default_reply = account_settings.get("default_reply") or {}
    if bool(default_reply.get("enabled")):
        content = _format_reply(str(default_reply.get("reply_content") or ""), message, item_id)
        if content:
            return AutoReplyMatch(
                reply=content,
                keyword="默认回复",
                rule_id=0,
                item_id=item_id,
                rule_type="default",
            )
    return None


async def match_auto_reply(account: Account, message: dict[str, Any]) -> AutoReplyMatch | None:
    """完整回复优先级：商品/通用关键词 > AI > 账号默认回复。"""
    keyword_match = await match_keyword_reply(account, message)
    if keyword_match is not None:
        return keyword_match
    text = str(message.get("text") or "").strip()
    if not text or _is_system_message(text) or _is_auto_delivery_message(text):
        return None
    if await _should_skip_reply(account, text):
        return None
    return await _match_ai_or_default_reply(account, message)


async def _send_text(account_id: int, cid: str, to_user_id: str, text: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{settings.websocket_service_url.rstrip('/')}/internal/chat/{account_id}/send-text",
            json={"cid": cid, "to_user_id": to_user_id, "text": text},
        )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not response.is_success or not body.get("success", True):
        detail = body.get("message") or body.get("detail") or f"HTTP {response.status_code}"
        raise RuntimeError(str(detail)[:500])
    data = body.get("data") or {}
    return str(data.get("messageId") or data.get("mid") or "")


async def _send_image(account: Account, cid: str, to_user_id: str, image_value: str) -> tuple[str, str]:
    from common.services.xianyu_platform import upload_chat_image

    value = str(image_value or "").strip()
    if value.startswith("http://") or value.startswith("https://"):
        image_url = value
    else:
        root = Path(settings.static_dir).resolve()
        local_path = root / value.removeprefix("/static/") if value.startswith("/static/") else Path(value).resolve()
        if root not in local_path.parents:
            raise RuntimeError("自动回复图片不在系统静态目录内")
        image_url = await upload_chat_image(str(local_path), account.cookie or "")
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            f"{settings.websocket_service_url.rstrip('/')}/internal/chat/{account.id}/send-image-url",
            json={"cid": cid, "to_user_id": to_user_id, "image_url": image_url},
        )
    body = response.json() if response.content else {}
    if not response.is_success or not body.get("success"):
        raise RuntimeError(str(body.get("message") or body.get("detail") or "闲鱼图片消息发送失败")[:500])
    data = body.get("data") or {}
    return image_url, str(data.get("messageId") or data.get("message_id") or "")


async def _record_sent_message(
    account: Account,
    cid: str,
    text: str,
    platform_message_id: str,
    *,
    message_type: str = "text",
    images: list[str] | None = None,
) -> None:
    """把已发送的自动回复回写到聊天流并广播给在线聊天页面。

    闲鱼发送接口只返回受理结果，部分响应没有平台消息 ID，因此使用本地
    唯一 ID 作为临时记录键。后续历史同步仍可补充平台原始记录；这一步的
    目的，是让“在线聊天”和统计看到已经实际发出的自动回复。
    """
    message_id = str(platform_message_id or "").strip() or f"auto-{int(time.time() * 1000)}-{uuid4().hex[:12]}"
    message = {
        "messageId": message_id,
        "senderId": str(account.goofish_id or account.id),
        "senderName": str(account.account_name or ""),
        "isSelf": True,
        "type": message_type,
        "text": text,
        "images": images or [],
        "time": int(time.time() * 1000),
    }
    event = {"event": "new_message", "cid": cid, "message": message}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                f"{settings.backend_service_url.rstrip('/')}/api/v1/chat-new/internal/events",
                headers={"X-Internal-Token": settings.jwt_secret},
                json={"account_id": int(account.id), "event": event},
            )
        if not response.is_success:
            logger.warning("账号 %s 自动回复已发送，但聊天记录回写失败：HTTP %s", account.id, response.status_code)
    except httpx.HTTPError as exc:
        logger.warning("账号 %s 自动回复已发送，但聊天记录回写异常：%s", account.id, str(exc)[:300])


async def _record_auto_reply_log(
    account: Account,
    cid: str,
    source_message: dict[str, Any],
    match: AutoReplyMatch,
    reply_text: str,
    *,
    send_status: str = "success",
    error_message: str = "",
    reply_image_url: str = "",
) -> None:
    """写入消息日志页面使用的真实自动回复明细。"""
    payload = {
        "account_pk": int(account.id),
        "account_id": str(account.goofish_id or account.id),
        "account_name": str(account.account_name or ""),
        "chat_id": cid,
        "item_id": match.item_id or None,
        "source_message_id": str(source_message.get("messageId") or source_message.get("message_id") or "") or None,
        "sender_user_id": str(source_message.get("senderId") or ""),
        "sender_user_name": str(source_message.get("senderName") or "") or None,
        "source_message": str(source_message.get("text") or ""),
        "source_message_time": source_message.get("time") or source_message.get("timestamp"),
        "process_status": "success" if send_status == "success" else "failed",
        "decision_reason": "关键词命中" if match.rule_type.startswith("keyword") else ("AI生成" if match.rule_type == "ai" else "默认回复"),
        "reply_strategy": match.rule_type,
        "reply_mode": "image" if match.response_type == "image" else "text",
        "matched_keyword": match.keyword if match.rule_type.startswith("keyword") else None,
        "matched_rule_type": match.rule_type,
        "ai_model_name": None,
        "ai_provider_name": None,
        "reply_text": reply_text,
        "reply_image_url": reply_image_url or None,
        "error_message": error_message or None,
        "send_status": send_status,
        "send_fail_reason": error_message or None,
    }
    if match.rule_type == "ai":
        async with async_session_maker() as db:
            platform_ai_settings = await load_platform_ai_settings(db, int(account.user_id))
        ai = platform_ai_settings.get("ai_settings") or {}
        payload["ai_model_name"] = ai.get("model_name") or None
        payload["ai_provider_name"] = provider_name(ai.get("provider_type"), ai.get("base_url"), ai.get("model_name"))
    try:
        async with async_session_maker() as db:
            db.add(
                FeatureRecord(
                    owner_id=int(account.user_id),
                    feature="auto-reply-logs",
                    external_id=str(source_message.get("messageId") or uuid4().hex),
                    status="active" if send_status == "success" else "failed",
                    payload=payload,
                    note="实时自动回复日志",
                )
            )
            await db.commit()
    except Exception as exc:
        logger.warning("账号 %s 自动回复日志写入失败：%s", account.id, str(exc)[:300])


async def _dispatch_auto_reply(account_id: int, cid: str, message: dict[str, Any]) -> None:
    """处理一条新买家消息；该函数由实时事件入口以后台任务方式调用。"""
    if bool(message.get("isSelf")):
        return
    text = str(message.get("text") or "").strip()
    sender_id = str(message.get("senderId") or "").strip()
    if not cid or not text or not sender_id:
        return

    account = await _load_account(account_id)
    if account is None or account.status != "active":
        logger.info("账号 %s 未启用，跳过自动回复", account_id)
        return

    match = await match_auto_reply(account, message)
    if match is None:
        logger.info("账号 %s 消息未命中自动回复规则：%s", account_id, text[:80])
        return
    if match.response_type == "text" and not match.reply:
        logger.info("账号 %s 命中关键词 %r，但回复内容为空", account_id, match.keyword)
        return
    if match.response_type == "image" and not match.image_url:
        logger.info("账号 %s 命中图片关键词 %r，但图片地址为空", account_id, match.keyword)
        return

    # 旧版支持用 ###### 把一条回复拆成多条发送，保留该行为。
    segments = [part.strip() for part in re.split(r"######", match.reply) if part.strip()]
    try:
        message_ids: list[str] = []
        if match.response_type == "image":
            image_url, sent_message_id = await _send_image(account, cid, sender_id, match.image_url)
            message_ids.append(sent_message_id)
            await _record_sent_message(account, cid, match.reply or "", sent_message_id, message_type="image", images=[image_url])
            if match.reply:
                sent_message_id = await _send_text(account_id, cid, sender_id, match.reply)
                message_ids.append(sent_message_id)
                await _record_sent_message(account, cid, match.reply, sent_message_id)
            await _record_auto_reply_log(account, cid, message, match, match.reply, reply_image_url=image_url)
        else:
            for index, segment in enumerate(segments):
                sent_message_id = await _send_text(account_id, cid, sender_id, segment)
                message_ids.append(sent_message_id)
                await _record_sent_message(account, cid, segment, sent_message_id)
                if index < len(segments) - 1:
                    await asyncio.sleep(0.5)
            await _record_auto_reply_log(account, cid, message, match, match.reply)
        logger.info(
            "账号 %s 自动回复已发送：关键词=%r，规则=%s，消息ID=%s",
            account_id,
            match.keyword,
            match.rule_id,
            ",".join(item for item in message_ids if item) or "平台已受理",
        )
    except Exception as exc:
        await _record_auto_reply_log(
            account,
            cid,
            message,
            match,
            match.reply,
            send_status="failed",
            error_message=str(exc)[:500],
        )
        logger.warning(
            "账号 %s 自动回复发送失败：关键词=%r，原因=%s",
            account_id,
            match.keyword,
            str(exc)[:500],
        )


async def dispatch_auto_reply(account_id: int, cid: str, message: dict[str, Any]) -> None:
    """后台任务安全入口，避免单条异常影响实时事件接收。"""
    try:
        await _dispatch_auto_reply(account_id, cid, message)
    except Exception as exc:  # pragma: no cover - 由实时服务触发的兜底保护
        logger.exception("账号 %s 自动回复处理异常：%s", account_id, str(exc)[:500])
