"""闲鱼 IM 内容载荷解析。

实时推送和历史消息都把真正的内容放在一层 JSON（或 base64 JSON）里，
统一在这里解码，避免实时聊天和历史聊天出现两套显示逻辑。
"""
from __future__ import annotations

import base64
import json
from typing import Any, Sequence


def load_content_json(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    if value.startswith("{"):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, dict):
                return decoded
        except (TypeError, ValueError):
            pass
    try:
        decoded = json.loads(base64.b64decode(value + "=" * (-len(value) % 4)).decode("utf-8"))
        return decoded if isinstance(decoded, dict) else None
    except (TypeError, ValueError, UnicodeDecodeError):
        return None


def _looks_like_content(value: Any) -> bool:
    return isinstance(value, dict) and (
        "contentType" in value or any(key in value for key in ("text", "image", "picUrl", "audio"))
    )


def parse_content_payloads(candidates: Sequence[Any]) -> tuple[str, list[str], str]:
    decoded = next((item for item in (load_content_json(candidate) for candidate in candidates) if _looks_like_content(item)), None)
    if not decoded:
        return "", [], "text"

    content_type = decoded.get("contentType", 0)
    if content_type == 1 or "text" in decoded:
        text = decoded.get("text", "")
        return (str(text.get("text", "")) if isinstance(text, dict) else str(text)), [], "text"
    if content_type == 2 or "image" in decoded or decoded.get("picUrl"):
        if decoded.get("picUrl"):
            return "", [str(decoded["picUrl"])], "image"
        image = decoded.get("image") or {}
        pics = image.get("pics", []) if isinstance(image, dict) else []
        urls = [str(pic.get("url")) for pic in pics if isinstance(pic, dict) and pic.get("url")]
        return "", urls, "image"
    if content_type == 3 or "audio" in decoded:
        return "[语音消息]", [], "text"
    return "", [], "text"


def interpret_content(decoded: dict[str, Any]) -> tuple[str, list[str], str]:
    """解释已经解码的内容对象，供历史消息接口复用。"""
    if not isinstance(decoded, dict):
        return "", [], "text"
    content_type = decoded.get("contentType", 0)
    if content_type == 1 or "text" in decoded:
        value = decoded.get("text", "")
        return (str(value.get("text", "")) if isinstance(value, dict) else str(value)), [], "text"
    if content_type == 2 or "image" in decoded or decoded.get("picUrl"):
        if decoded.get("picUrl"):
            return "", [str(decoded["picUrl"])], "image"
        image = decoded.get("image") or {}
        pics = image.get("pics", []) if isinstance(image, dict) else []
        return "", [str(pic.get("url")) for pic in pics if isinstance(pic, dict) and pic.get("url")], "image"
    if content_type == 3 or "audio" in decoded:
        return "[语音消息]", [], "text"
    return "", [], "text"
