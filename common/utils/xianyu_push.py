"""闲鱼 IM 主动推送的解码与标准化。"""
from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import unquote

from common.utils.xianyu_message_parser import parse_content_payloads


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value) if value is not None else ""


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {_text(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return _text(value) if isinstance(value, bytes) else value


def decode_push_data(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, str) or not data:
        return None
    try:
        raw = base64.b64decode(data + "=" * (-len(data) % 4))
    except (TypeError, ValueError):
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
        if isinstance(parsed, dict):
            # chatType 是 IM 的系统提示信封，不是可展示的聊天消息。
            return None if "chatType" in parsed else parsed
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        import msgpack

        parsed = msgpack.unpackb(raw, raw=False, strict_map_key=False)
        return _normalize(parsed) if isinstance(parsed, dict) else None
    except Exception:
        return None


def _strip_goofish(value: Any) -> str:
    return _text(value).split("@", 1)[0]


def _json_object(value: Any) -> dict[str, Any]:
    """Best-effort decode of JSON fields in Xianyu push envelopes."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _extract_message_id(msg: dict[str, Any]) -> str:
    """Extract the platform message ID from all known IM push layouts."""
    if not isinstance(msg, dict):
        return ""
    direct = msg.get("3")
    if direct not in (None, "") and not isinstance(direct, (dict, list)):
        return _text(direct)

    msg_1 = msg.get("1") if isinstance(msg.get("1"), dict) else {}
    msg_10 = msg_1.get("10") if isinstance(msg_1.get("10"), dict) else {}
    for value in (msg_10.get("bizTag"), msg_10.get("extJson")):
        decoded = _json_object(value)
        message_id = decoded.get("messageId") or decoded.get("msgId")
        if message_id not in (None, ""):
            return _text(message_id)

    msg_4 = msg.get("4") if isinstance(msg.get("4"), dict) else {}
    decoded = _json_object(msg_4.get("extJson"))
    message_id = decoded.get("messageId") or decoded.get("msgId")
    return _text(message_id) if message_id not in (None, "") else ""


def _extract_item_id(msg: dict[str, Any]) -> str:
    """从实时推送信封中提取商品 ID，供商品级关键词规则使用。"""
    msg_1 = msg.get("1") if isinstance(msg.get("1"), dict) else {}
    msg_10 = msg_1.get("10") if isinstance(msg_1.get("10"), dict) else {}
    for value in (msg_10.get("reminderUrl"), msg_10.get("itemUrl")):
        value = _text(value)
        if "itemId=" in value:
            item_id = value.split("itemId=", 1)[1].split("&", 1)[0]
            if item_id:
                return item_id
    for value in (msg_10.get("bizTag"), msg_10.get("extJson")):
        if not value:
            continue
        try:
            decoded = json.loads(_text(value)) if isinstance(value, str) else value
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = {}
        if isinstance(decoded, dict) and decoded.get("itemId"):
            return _text(decoded["itemId"])
    content = msg_1.get("6") if isinstance(msg_1.get("6"), dict) else {}
    content = content.get("3") if isinstance(content.get("3"), dict) else {}
    raw_card = content.get("5")
    if raw_card:
        try:
            decoded = json.loads(_text(raw_card))
            jump_url = (
                decoded.get("dxCard", {})
                .get("item", {})
                .get("main", {})
                .get("exContent", {})
                .get("button", {})
                .get("intent", {})
                .get("page", {})
                .get("jumpUrl", "")
            )
            if "itemId=" in str(jump_url):
                return str(jump_url).split("itemId=", 1)[1].split("&", 1)[0]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return ""


def extract_item_id(msg: dict[str, Any]) -> str:
    """公开商品 ID 提取入口，供实时事件和历史消息标准化共用。"""
    if not isinstance(msg, dict):
        return ""

    def walk(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("itemId", "item_id", "itemID"):
                candidate = value.get(key)
                if candidate not in (None, "") and re.fullmatch(r"\d{6,}", str(candidate).strip()):
                    return str(candidate).strip()
            for key in ("targetUrl", "target_url", "itemUrl", "item_url", "jumpUrl", "jump_url", "reminderUrl"):
                candidate = str(value.get(key) or "")
                match = re.search(r"(?:itemId|item_id)[=:]\s*['\"]?(\d{6,})", unquote(candidate), flags=re.IGNORECASE)
                if match:
                    return match.group(1)
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, str) and value:
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = None
            if isinstance(decoded, (dict, list)):
                return walk(decoded)
            match = re.search(r"(?:itemId|item_id)[=:]\s*['\"]?(\d{6,})", unquote(value), flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return ""

    return walk(msg) or _extract_item_id(msg)


def extract_order_id(msg: dict[str, Any]) -> str:
    """从闲鱼实时消息原始载荷中提取订单号。

    付款提醒的展示文本通常只有“已付款，待发货”，订单号藏在卡片的
    targetUrl、bizTag 或 extJson 中。只保存标准化文本会丢失这个关键字段，
    进而无法在订单列表接口受限时触发自动发货。
    """
    if not isinstance(msg, dict):
        return ""

    def from_value(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("orderId", "order_id", "bizOrderId", "biz_order_id", "orderNo", "order_no"):
                candidate = value.get(key)
                if candidate not in (None, "") and re.fullmatch(r"\d{8,}", str(candidate).strip()):
                    return str(candidate).strip()
            for key in ("targetUrl", "target_url", "orderUrl", "order_url", "jumpUrl", "jump_url", "reminderUrl"):
                candidate = str(value.get(key) or "")
                if candidate:
                    found = from_text(candidate)
                    if found:
                        return found
            for child in value.values():
                found = from_value(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = from_value(child)
                if found:
                    return found
        elif isinstance(value, str) and value:
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = None
            if isinstance(decoded, (dict, list)):
                return from_value(decoded)
            return from_text(value)
        return ""

    def from_text(value: str) -> str:
        decoded = unquote(str(value or ""))
        patterns = (
            r"(?:orderId|order_id|bizOrderId|biz_order_id|orderNo|order_no)[=:]\s*['\"]?(\d{8,})",
            r"order_detail(?:\?|%3F)id[=:]\s*['\"]?(\d{8,})",
        )
        for pattern in patterns:
            match = re.search(pattern, decoded, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return ""

    direct = from_value(msg)
    if direct:
        return direct
    return from_text(json.dumps(msg, ensure_ascii=False, separators=(",", ":")))


def _message(msg: dict[str, Any], myid: str, *, sender_id: str, sender_name: str, cid: str, text: str, images: list[str], msg_type: str, timestamp: Any, item_id: str = "") -> dict[str, Any]:
    return {
        "event": "new_message",
        "cid": cid,
        "message": {
            "messageId": _extract_message_id(msg),
            "senderId": sender_id,
            "senderName": sender_name,
            "isSelf": sender_id == myid,
            "type": msg_type if msg_type in {"text", "image", "card"} else "text",
            "text": text,
            "images": images,
            "time": int(timestamp or 0),
            "itemId": item_id,
            "orderId": extract_order_id(msg),
        },
    }


def parse_push_message(msg: dict[str, Any], myid: str) -> dict[str, Any] | None:
    msg_1 = msg.get("1")
    if isinstance(msg_1, dict):
        msg_10 = msg_1.get("10") or {}
        if isinstance(msg_10, dict) and msg_10.get("reminderContent") is not None:
            msg_6 = msg_1.get("6") or {}
            msg_6_3 = msg_6.get("3") if isinstance(msg_6, dict) else {}
            msg_6_3 = msg_6_3 if isinstance(msg_6_3, dict) else {}
            text, images, msg_type = parse_content_payloads([msg_6_3.get("5"), msg_6_3.get("1")])
            if not text and not images:
                text, msg_type = _text(msg_10.get("reminderContent")), "text"
            return _message(
                msg,
                myid,
                sender_id=_strip_goofish(msg_10.get("senderUserId")),
                sender_name=_text(msg_10.get("senderNick") or msg_10.get("reminderTitle")),
                cid=_strip_goofish(msg_1.get("2", "")),
                text=text,
                images=images,
                msg_type=msg_type,
                timestamp=msg_1.get("5", 0),
                item_id=_extract_item_id(msg),
            )
        msg_6 = msg_1.get("6") or {}
        if isinstance(msg_6, dict) and isinstance(msg_6.get("3"), dict):
            card = msg_6["3"]
            return _message(
                msg,
                myid,
                sender_id=_strip_goofish((msg_1.get("1") or {}).get("1", "") if isinstance(msg_1.get("1"), dict) else ""),
                sender_name="系统",
                cid=_strip_goofish(msg_1.get("2", "")),
                text=_text(card.get("2") or "[卡片消息]"),
                images=[],
                msg_type="card",
                timestamp=msg_1.get("5", 0),
                item_id=_extract_item_id(msg),
            )
    if isinstance(msg.get("1"), str) and isinstance(msg.get("4"), dict) and "reminderContent" in msg["4"]:
        detail = msg["4"]
        return _message(
            msg,
            myid,
            sender_id=_strip_goofish(detail.get("senderUserId")),
            sender_name=_text(detail.get("reminderTitle") or "系统"),
            cid=_strip_goofish(msg.get("2", "")),
            text=_text(detail.get("reminderContent")),
            images=[],
            msg_type="text",
            timestamp=msg.get("5", 0),
            item_id=_extract_item_id(msg),
        )
    return None


def extract_events(message: dict[str, Any], myid: str) -> list[dict[str, Any]]:
    body = message.get("body") or {}
    package = body.get("syncPushPackage") if isinstance(body, dict) else None
    values = package.get("data", []) if isinstance(package, dict) else []
    events: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        decoded = decode_push_data(item.get("data"))
        if decoded:
            parsed = parse_push_message(decoded, myid)
            if parsed:
                events.append(parsed)
    return events
