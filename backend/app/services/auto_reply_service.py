"""实时聊天自动回复服务。

关键词配置和实时 IM 连接分别由后端、WebSocket 服务维护。本模块负责把两者
接起来：收到买家消息后，从当前账号的结构化关键词规则中匹配回复，并通过
WebSocket 内部发送接口发回闲鱼。
"""
from __future__ import annotations

import asyncio
import logging
import re
import string
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
from backend.app.services.builtin_keyword_service import load_builtin_keyword_rules
from backend.app.services.entitlements import FEATURE_AI_SMART_REPLY, FEATURE_BUILTIN_AI_REPLY, get_effective_entitlement

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
    priority: int = 0
    keyword_length: int = 0
    match_mode: str = "contains"
    conversation_stage: str = ""


_STAGE_ALIASES = {
    "pre_sale": {"pre_sale", "presale", "售前", "售前咨询", "未下单", "咨询"},
    "paid_pending_delivery": {"paid_pending_delivery", "paid", "已付款", "已付款待发货", "待发货", "付款待发货"},
    "delivered_pending_receipt": {"delivered_pending_receipt", "delivered", "已发货", "已发货待收货", "待收货"},
    "after_sale": {"after_sale", "售后", "退款", "退货", "纠纷"},
}
# 旧版公开的 send_user_* 变量即使没有昵称也会替换为空字符串；
# 新增的业务事实变量则必须有值，避免把不完整的商品信息发给买家。
_REQUIRED_TEMPLATE_FIELDS = {
    "buyer_name", "buyer_nick", "buyer_id", "item_title", "item_price", "stock", "item_id",
}


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


def _message_value(message: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = message.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _format_reply(reply: str, message: dict[str, Any], item_id: str) -> str | None:
    """替换回复变量；变量缺失时拒绝发送，避免把模板占位符发给买家。"""
    if not reply:
        return ""
    values = {
        "send_user_name": _message_value(message, "senderName", "sender_name", "buyer_name", "buyer_nick"),
        "send_user_id": _message_value(message, "senderId", "sender_id", "buyer_id"),
        "send_message": _message_value(message, "text", "message", "content"),
        "buyer_name": _message_value(message, "buyer_name", "buyerName", "senderName", "sender_name", "buyer_nick"),
        "buyer_nick": _message_value(message, "buyer_nick", "buyerName", "senderName", "sender_name", "buyer_name"),
        "buyer_id": _message_value(message, "buyer_id", "buyerId", "senderId", "sender_id"),
        "item_title": _message_value(message, "item_title", "itemTitle", "title", "goodsTitle"),
        "item_price": _message_value(message, "item_price", "itemPrice", "price"),
        "price": _message_value(message, "price", "item_price", "itemPrice"),
        "stock": _message_value(message, "stock", "inventory", "quantity"),
        "item_id": item_id,
        "order_id": _message_value(message, "order_id", "orderId"),
        "DELIVERY_CONTENT": _message_value(message, "delivery_content", "deliveryContent"),
    }
    fields = {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(reply)
        if field_name
    }
    unknown = sorted(field for field in fields if field not in values)
    missing = sorted(field for field in fields if field in _REQUIRED_TEMPLATE_FIELDS and not values[field])
    if unknown or missing:
        logger.warning(
            "自动回复模板未发送：未识别变量=%s，缺失变量=%s",
            ",".join(unknown) or "无",
            ",".join(missing) or "无",
        )
        return None
    try:
        return reply.format(**values).strip()
    except (KeyError, IndexError, ValueError):
        logger.warning("自动回复变量替换失败，已跳过该规则")
        return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled", "是", "启用"}:
        return True
    if text in {"0", "false", "no", "off", "disabled", "否", "停用", ""}:
        return False
    return default


def _is_disabled_rule(payload: dict[str, Any]) -> bool:
    """兼容不同导入格式的启用/审核字段。旧规则没有这些字段，默认可用。"""
    for key in ("enabled", "is_enabled"):
        if key in payload and payload.get(key) is False:
            return True
        if key in payload and str(payload.get(key)).strip().lower() in {"0", "false", "off", "disabled"}:
            return True
    if _as_bool(payload.get("needs_human")):
        return True
    status = str(payload.get("approval_status") or payload.get("review_status") or "").strip().lower()
    return status in {"draft", "review", "review_required", "pending", "disabled", "rejected"}


def _canonical_stage(value: Any) -> str:
    text = _normalized(value)
    if not text:
        return ""
    for canonical, aliases in _STAGE_ALIASES.items():
        if text == _normalized(canonical) or text in {_normalized(alias) for alias in aliases}:
            return canonical
    return text


def _message_stage(message: dict[str, Any]) -> str:
    return _canonical_stage(_message_value(
        message,
        "conversation_stage",
        "conversationStage",
        "order_stage",
        "orderStage",
        "stage",
    ))


def _rule_stages(payload: dict[str, Any]) -> set[str]:
    value = payload.get("conversation_stage", payload.get("conversationStage", payload.get("stage", payload.get("stages"))))
    if value is None or value == "":
        return set()
    values = value if isinstance(value, (list, tuple, set)) else re.split(r"[,，|]", str(value))
    return {_canonical_stage(item) for item in values if _canonical_stage(item)}


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


async def _load_rules(account: Account) -> list[Any]:
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
        filtered = [
            row
            for row in rows
            if str((row.payload or {}).get("account_id") or "") == str(account.id)
        ]
        platform_settings = await load_platform_ai_settings(db, int(account.user_id))
        if not platform_settings.get("builtin_ai_reply_enabled"):
            return filtered
        owner = await db.get(User, int(account.user_id))
        principal = {
            "sub": str(account.user_id),
            "role": owner.role if owner is not None else "user",
            "plan_code": owner.plan_code if owner is not None else "NORMAL",
        }
        entitlement = await get_effective_entitlement(db, principal, FEATURE_BUILTIN_AI_REPLY)
        if not entitlement.enabled:
            return filtered
        return filtered + list(load_builtin_keyword_rules())


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
    """按商品范围、优先级、匹配精度和关键词长度选择最合适的规则。

    旧规则没有 priority/stage 等字段，因此仍按原来的通用关键词逻辑工作；
    新规则可通过这些字段避免短关键词抢先命中，或限制在指定会话阶段生效。
    """
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
    message_stage = _message_stage(message)
    normalized_text = _normalized(text)
    candidates: list[tuple[FeatureRecord, bool, str, int, int, int, str]] = []
    for row in rules:
        payload = row.payload or {}
        if _is_disabled_rule(payload):
            continue
        rule_item_id = str(payload.get("item_id") or "").strip()
        rule_stages = _rule_stages(payload)
        if rule_stages and (not message_stage or message_stage not in rule_stages):
            continue
        if rule_item_id:
            if item_id and rule_item_id == item_id:
                is_item_rule = True
            else:
                continue
        else:
            is_item_rule = False
        priority = _as_int(payload.get("priority"), 0)
        configured_match_mode = str(payload.get("match_mode") or payload.get("matchMode") or "contains").strip().lower()
        for keyword in split_keyword_lines(payload.get("keyword")):
            normalized_keyword = _normalized(keyword)
            if not normalized_keyword:
                continue
            match_mode = configured_match_mode if configured_match_mode in {"exact", "prefix", "contains"} else "contains"
            if match_mode == "exact":
                matched = normalized_text == normalized_keyword
            elif match_mode == "prefix":
                matched = normalized_text.startswith(normalized_keyword)
            else:
                match_mode = "contains"
                matched = normalized_keyword in normalized_text
            if matched:
                candidates.append((row, is_item_rule, keyword, len(normalized_keyword), priority, int(row.id), match_mode))

    # 商品规则 > 显式优先级 > 精确/前缀 > 长关键词 > 旧规则 ID。
    # 这仍然保留“contains”模糊匹配，但让“售后”不会抢在“售后退款”之前。
    candidates.sort(key=lambda pair: (
        not pair[1],
        -pair[4],
        {"exact": 0, "prefix": 1, "contains": 2}.get(pair[6], 2),
        -pair[3],
        pair[5],
    ))
    for row, is_item_rule, keyword, keyword_length, priority, _, match_mode in candidates:
        payload = row.payload or {}
        response_type = str(payload.get("type") or "text").lower()
        rule_type = "keyword_item" if is_item_rule else "keyword_common"
        if response_type == "image":
            image_url = str(payload.get("image_url") or payload.get("imageUrl") or "").strip()
            if not image_url:
                logger.warning("关键词规则 %s 图片地址为空，跳过回复", row.id)
                continue
            description = _format_reply(str(payload.get("description") or ""), message, item_id)
            if description is None:
                continue
            return AutoReplyMatch(
                reply=description,
                keyword=keyword,
                rule_id=int(row.id),
                item_id=str(payload.get("item_id") or ""),
                rule_type=rule_type,
                response_type="image",
                image_url=image_url,
                priority=priority,
                keyword_length=keyword_length,
                match_mode=match_mode,
                conversation_stage=message_stage,
            )
        raw_reply = str(payload.get("reply") or "")
        reply_message = message
        if payload.get("builtin"):
            # 内置话术允许在没有商品详情上下文的聊天入口先给出基础答复，
            # 同时不会把模板占位符原样发送给买家。
            reply_message = dict(message)
            reply_message.setdefault("item_title", "这款商品")
            reply_message.setdefault("price", "页面价格")
            reply_message.setdefault("item_price", "页面价格")
        reply = _format_reply(raw_reply, reply_message, item_id)
        if reply is None:
            continue
        return AutoReplyMatch(
            reply=reply,
            keyword=keyword,
            rule_id=int(row.id),
            item_id=str(payload.get("item_id") or ""),
            rule_type=rule_type,
            priority=priority,
            keyword_length=keyword_length,
            match_mode=match_mode,
            conversation_stage=message_stage,
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
            headers={"X-Internal-Token": settings.jwt_secret},
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
            headers={"X-Internal-Token": settings.jwt_secret},
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
        "match_priority": match.priority,
        "match_keyword_length": match.keyword_length,
        "match_mode": match.match_mode,
        "conversation_stage": match.conversation_stage or None,
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
