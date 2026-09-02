# -*- coding: utf-8 -*-
"""本地自动回复路由。

先按工作区配置的关键词规则匹配；没有命中时返回未匹配，交给上层决定是否
调用外部 AI 服务。这样即使没有第三方密钥，规则回复仍然可以独立工作。
"""
from typing import Any


async def route_reply(message: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    data = context or {}
    normalized = message.casefold()
    for rule in data.get("keyword_rules", []):
        if not isinstance(rule, dict) or not rule.get("status", 1):
            continue
        keyword = str(rule.get("keyword", ""))
        matched = normalized == keyword.casefold() if rule.get("match_mode") == "exact" else keyword.casefold() in normalized
        if matched:
            return {"reply": str(rule.get("reply", "")), "matched": True, "source": "keyword", "keyword": keyword}
    default_reply = data.get("default_reply")
    if isinstance(default_reply, dict) and default_reply.get("content"):
        return {"reply": str(default_reply["content"]), "matched": True, "source": "default"}
    return {"reply": "", "matched": False, "source": "unmatched", "message": message}
