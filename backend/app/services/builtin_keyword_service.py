# -*- coding: utf-8 -*-
"""VIP 内置关键词回复规则。

内置规则来自仓库中的蒸馏话术包，不写入用户自己的 FeatureRecord，避免
把平台预置内容和用户编辑内容混在一起。开启后按平台账号共享，并以只读
规则的形式展示在关键词列表中。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BuiltinKeywordRule:
    id: int
    payload: dict[str, Any]


# 蒸馏包中保留的高频基础变种；这些短句必须覆盖，否则买家只发“你好”
# 或“能拍么”时会因为没有商品上下文而完全没有回复。
_BASIC_VARIANTS = (
    {
        "keywords": ["能拍么", "能拍吗", "可以拍吗", "现在能拍吗", "怎么发货"],
        "reply": "可以的亲，库存以系统显示为准，能正常拍下就是有货，付款后按系统方式发货，请留意订单和消息哦~",
        "priority": 92,
        "scene": "【售前咨询】能否下单与发货方式",
    },
    {
        "keywords": ["使用说明", "有使用说明吗", "有说明吗", "教程吗", "怎么用", "咋用"],
        "reply": "亲，具体使用说明和步骤以商品详情及实际交付内容为准，需要的话可以联系客服帮您核实哦~",
        "priority": 88,
        "scene": "【售前咨询】使用说明与教程",
    },
)


def _pack_path() -> Path:
    return Path(__file__).resolve().parents[3] / "dialogue_packs" / "distilled" / "distilled_pack.json"


def _import_path() -> Path:
    return Path(__file__).resolve().parents[3] / "dialogue_packs" / "distilled" / "distilled_keywords_import.json"


def _as_keywords(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    else:
        values = str(value or "").replace("\r", "\n").split("\n")
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        keyword = str(item or "").strip()
        if keyword and keyword.casefold() not in seen:
            result.append(keyword)
            seen.add(keyword.casefold())
    return result


def _priority(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@lru_cache(maxsize=1)
def load_builtin_keyword_rules() -> tuple[BuiltinKeywordRule, ...]:
    """加载并规范化蒸馏包；文件不存在时安全降级为空规则。"""
    path = _pack_path()
    try:
        source = json.loads(path.read_text(encoding="utf-8"))
        records = source.get("templates") if isinstance(source, dict) else source
    except (OSError, ValueError, TypeError):
        try:
            records = json.loads(_import_path().read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            records = []

    rules: list[BuiltinKeywordRule] = []
    records = list(records or []) + list(_BASIC_VARIANTS)
    for index, record in enumerate(records or [], start=1):
        if not isinstance(record, dict):
            continue
        keywords = _as_keywords(record.get("keywords", record.get("keyword")))
        reply = str(record.get("reply") or "").strip()
        if not keywords or not reply:
            continue
        payload = {
            "account_id": "",
            "keyword": "\n".join(keywords),
            "reply": reply,
            "item_id": str(record.get("item_id") or "").strip(),
            "type": "text",
            "priority": _priority(record.get("priority")),
            "match_mode": str(record.get("match_mode") or "contains").strip().lower(),
            "conversation_stage": str(record.get("conversation_stage") or "").strip(),
            "enabled": True,
            "needs_human": bool(record.get("needs_human", False)),
            "approval_status": "approved",
            "scene": str(record.get("scene") or record.get("scenario") or "").strip(),
            "source": "builtin",
            "builtin": True,
            "read_only": True,
        }
        if payload["match_mode"] not in {"contains", "prefix", "exact"}:
            payload["match_mode"] = "contains"
        # 负数 ID 与数据库规则隔离，同时保持规则顺序稳定。
        rules.append(BuiltinKeywordRule(id=-index, payload=payload))
    return tuple(rules)


def builtin_keyword_payloads() -> list[dict[str, Any]]:
    """返回供关键词列表使用的副本，避免调用方修改缓存。"""
    return [dict(rule.payload) | {"id": f"builtin-{abs(rule.id)}"} for rule in load_builtin_keyword_rules()]
