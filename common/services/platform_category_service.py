# -*- coding: utf-8 -*-
"""闲鱼商品分类推荐服务。

商品发布页的分类不是本地字典，而是由闲鱼卖家工作台根据标题、描述和
当前属性卡动态返回。本模块保留旧版实际使用的 mtop 请求协议，并把响应
中的分类候选、分类卡和动态属性转换为前端发布页使用的字段。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from http.cookies import SimpleCookie
from typing import Any

import httpx

from common.config import settings
from common.services.goofish_mtop import parse_cookie_string, serialize_cookies


logger = logging.getLogger("xr.platform_category")

APP_KEY = "34839810"
CATEGORY_API = "mtop.taobao.idle.kgraph.pc.property.recommend"
CATEGORY_URL = f"{settings.goofish_mtop_host}/h5/{CATEGORY_API}/2.0/"
REQUEST_TIMEOUT = httpx.Timeout(connect=15, read=30, write=20, pool=30)

TOKEN_ERROR_MARKERS = (
    "FAIL_SYS_TOKEN_EXOIRED",
    "FAIL_SYS_TOKEN_EXPIRED",
    "FAIL_SYS_TOKEN_EMPTY",
)


class CategoryRecommendationError(RuntimeError):
    """分类接口不可用或没有返回有效分类时抛出的业务异常。"""


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _sign(timestamp: str, token: str, body: str) -> str:
    return hashlib.md5(f"{token}&{timestamp}&{APP_KEY}&{body}".encode("utf-8")).hexdigest()


def _merge_response_cookie(original: str, response: httpx.Response) -> str:
    """把闲鱼响应下发的新令牌合并回当前 Cookie。"""
    merged = parse_cookie_string(original)
    for header in response.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        try:
            parsed.load(header)
        except Exception:  # pragma: no cover - 仅防御异常的 Set-Cookie
            continue
        for name, morsel in parsed.items():
            if morsel.value:
                merged[name] = morsel.value
    return serialize_cookies(merged)


def _ret_message(ret: Any) -> str:
    if isinstance(ret, list) and ret:
        return str(ret[0])
    if isinstance(ret, str) and ret:
        return ret
    return "闲鱼接口未返回明确结果"


def _is_success(ret: Any) -> bool:
    return isinstance(ret, list) and any(str(item).startswith("SUCCESS") for item in ret)


def _is_token_error(ret: Any) -> bool:
    text = " ".join(str(item) for item in ret) if isinstance(ret, list) else str(ret)
    upper = text.upper()
    return any(marker in upper for marker in TOKEN_ERROR_MARKERS)


def _card_list(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data") or {}
    if not isinstance(data, dict):
        return []
    cards = data.get("cardList") or []
    if isinstance(cards, str):
        try:
            cards = json.loads(cards)
        except json.JSONDecodeError:
            return []
    return [card for card in cards if isinstance(card, dict)] if isinstance(cards, list) else []


def _build_path(value: dict[str, Any]) -> list[dict[str, str]]:
    path: list[dict[str, str]] = []

    def add(category_id: str, category_name: str) -> None:
        item = {"id": category_id, "name": category_name}
        if category_id and category_name and (not path or path[-1] != item):
            path.append(item)

    levels: list[tuple[int, str, str]] = []
    for key, raw_id in value.items():
        match = re.fullmatch(r"channelCat(\d+)Id", str(key))
        if not match:
            continue
        level = int(match.group(1))
        category_id = _text(raw_id)
        category_name = _text(value.get(f"channelCat{level}Name"))
        if category_id and category_name:
            levels.append((level, category_id, category_name))
    for _, category_id, category_name in sorted(levels):
        add(category_id, category_name)

    channel_id = _text(value.get("channelCatId"))
    channel_name = _text(value.get("channelCatName"))
    add(channel_id, channel_name)
    # 没有频道路径时，普通 catId/catName 就是可展示的末级分类。
    if not path:
        add(_text(value.get("catId")), _text(value.get("catName")))
    return path


def _parse_current_card_list(response: dict[str, Any]) -> list[dict[str, Any]]:
    """保留分类、品牌、成色及属性卡，供下一次切换分类时原样回传。"""
    result: list[dict[str, Any]] = []
    for card in _card_list(response):
        card_data = card.get("cardData")
        if isinstance(card_data, dict) and _text(card_data.get("propertyId")):
            result.append(card_data)
    return result


def _parse_candidates(response: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for card in _card_list(response):
        card_data = card.get("cardData")
        if not isinstance(card_data, dict) or _text(card_data.get("propertyId")) != "-10000":
            continue
        values = card_data.get("valuesList") or []
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            transport = value.get("transportData") if isinstance(value.get("transportData"), dict) else {}
            channel_id = _text(value.get("channelCatId")) or _text(transport.get("channelCateId"))
            channel_name = _text(value.get("channelCatName")) or _text(transport.get("channelCateName"))
            category_name = _text(value.get("catName")) or _text(transport.get("valueName"))
            tb_id = _text(value.get("tbCatId")) or _text(transport.get("tbCatId"))
            normalized = {**transport, **value, "channelCatId": channel_id, "channelCatName": channel_name, "catName": category_name, "tbCatId": tb_id}
            path = _build_path(normalized)
            if not path and channel_id and channel_name:
                path = [{"id": channel_id, "name": channel_name}]
            if not path:
                continue
            result.append({
                "cat_id": _text(value.get("catId")) or _text(transport.get("catId")) or None,
                "cat_name": category_name or None,
                "channel_cat_id": channel_id or None,
                "channel_cat_name": channel_name or None,
                "leaf_id": _text(value.get("leafId")) or _text(transport.get("leafId")) or None,
                "tb_cat_id": tb_id or None,
                "path": path,
                "score": value.get("score"),
                "is_selected": _bool(value.get("isClicked")) or _bool(value.get("isUserClick")),
            })
    return result


def _apply_predict_result(response: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    data = response.get("data") or {}
    result = data.get("categoryPredictResult") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        return
    cat_id = _text(result.get("catId"))
    cat_name = _text(result.get("catName"))
    channel_id = _text(result.get("channelCatId"))
    tb_id = _text(result.get("tbCatId"))
    if not any((cat_id, cat_name, channel_id, tb_id)):
        return
    for candidate in candidates:
        matches = (
            bool(channel_id and candidate.get("channel_cat_id") == channel_id)
            or bool(tb_id and candidate.get("tb_cat_id") == tb_id)
            or bool(cat_id and candidate.get("cat_id") == cat_id)
            or bool(cat_name and candidate.get("cat_name") == cat_name)
        )
        if matches:
            candidate["cat_id"] = candidate.get("cat_id") or cat_id or None
            candidate["cat_name"] = candidate.get("cat_name") or cat_name or None
            candidate["channel_cat_id"] = candidate.get("channel_cat_id") or channel_id or None
            candidate["tb_cat_id"] = candidate.get("tb_cat_id") or tb_id or None
            candidate["is_selected"] = True
            break


def _parse_properties(response: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for card in _card_list(response):
        card_data = card.get("cardData")
        if not isinstance(card_data, dict):
            continue
        property_id = _text(card_data.get("propertyId"))
        property_name = _text(card_data.get("propertyName"))
        if not property_id or property_id == "-10000" or not property_name:
            continue
        options: list[dict[str, Any]] = []
        values = card_data.get("valuesList") or []
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, dict):
                    continue
                transport = value.get("transportData") if isinstance(value.get("transportData"), dict) else {}
                value_id = _text(value.get("valueId")) or _text(transport.get("valueId"))
                value_name = _text(value.get("valueName")) or _text(transport.get("valueName"))
                if not value_name:
                    continue
                options.append({
                    "property_id": property_id,
                    "property_name": property_name,
                    "value_id": value_id or None,
                    "value_name": value_name,
                    "channel_cat_id": _text(value.get("channelCatId")) or _text(transport.get("channelCateId")) or None,
                    "tb_cat_id": _text(value.get("tbCatId")) or _text(transport.get("tbCatId")) or None,
                })
        result.append({
            "property_id": property_id,
            "property_name": property_name,
            "input_word": _text(card_data.get("inputWord")) or None,
            "is_multiple": _bool(card_data.get("isMultiple")),
            "is_decisive_property": _bool(card_data.get("isDecisiveProperty")),
            "options": options,
        })
    return result


class PlatformCategoryService:
    """调用闲鱼卖家工作台分类推荐接口。"""

    async def recommend(
        self,
        *,
        title: str,
        description: str,
        cookie: str,
        account_id: str = "",
        current_card_list: list[dict[str, Any]] | None = None,
        selected_list: list[dict[str, Any]] | None = None,
        cat_id: str = "",
        cat_name: str = "",
        channel_cat_id: str = "",
        proxy: str | None = None,
    ) -> dict[str, Any]:
        current_cookie = str(cookie or "").replace("\r", "").replace("\n", "").strip()
        if not parse_cookie_string(current_cookie):
            raise CategoryRecommendationError("闲鱼账号缺少有效 Cookie，请先重新登录账号")

        payload: dict[str, Any] = {
            "title": title.strip(),
            "lockCpv": False,
            "multiSKU": False,
            "publishScene": "pcBackendPublish",
            "scene": "shopPcPublish",
            "description": description.strip(),
            "uniqueCode": f"{int(time.time() * 1000)}{str(account_id)[-4:]}",
        }
        if current_card_list:
            payload["currentCardList"] = current_card_list
        if selected_list:
            payload["selectedList"] = selected_list
        if cat_id:
            payload["catId"] = cat_id
        if cat_name:
            payload["catName"] = cat_name
        if channel_cat_id:
            payload["channelCatId"] = channel_cat_id
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True, proxy=proxy or settings.goofish_proxy or None) as client:
                for attempt in range(2):
                    cookies = parse_cookie_string(current_cookie)
                    timestamp = str(int(time.time() * 1000))
                    token = cookies.get("_m_h5_tk", "").split("_", 1)[0]
                    params = {
                        "jsv": "2.7.2",
                        "appKey": APP_KEY,
                        "t": timestamp,
                        "sign": _sign(timestamp, token, body),
                        "v": "2.0",
                        "type": "originaljson",
                        "accountSite": "xianyu",
                        "dataType": "json",
                        "timeout": "20000",
                        "api": CATEGORY_API,
                        "sessionOption": "AutoLoginOnly",
                        "spm_cnt": "a21107h.42829679.0.0",
                        "spm_pre": "a21107h.42829791.0.0",
                    }
                    headers = {
                        "Accept": "application/json, text/plain, */*",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": "https://seller.goofish.com",
                        "Referer": "https://seller.goofish.com/",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                        "idle_site_biz_code": "COMMONPRO",
                        "idle_user_group_member_id": "",
                        "Cookie": current_cookie,
                    }
                    response = await client.post(CATEGORY_URL, params=params, data={"data": body}, headers=headers, cookies=cookies)
                    response.raise_for_status()
                    next_cookie = _merge_response_cookie(current_cookie, response)
                    try:
                        result = response.json()
                    except ValueError as exc:
                        raise CategoryRecommendationError("分类推荐接口返回格式异常，请稍后重试") from exc
                    if not isinstance(result, dict):
                        raise CategoryRecommendationError("分类推荐接口返回格式异常，请稍后重试")
                    ret = result.get("ret") or []
                    if _is_token_error(ret) and attempt == 0 and next_cookie != current_cookie:
                        current_cookie = next_cookie
                        continue
                    if _is_token_error(ret):
                        raise CategoryRecommendationError("闲鱼账号令牌已过期，请重新登录账号")
                    if not _is_success(ret):
                        raise CategoryRecommendationError("分类推荐失败，请检查账号登录状态后重试")

                    candidates = _parse_candidates(result)
                    _apply_predict_result(result, candidates)
                    if not candidates:
                        raise CategoryRecommendationError("接口未返回可用的商品分类")
                    return {
                        "candidates": candidates,
                        "properties": _parse_properties(result),
                        "card_list": _parse_current_card_list(result),
                        "account_id": account_id,
                        "cookies_str": next_cookie,
                    }
        except CategoryRecommendationError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("分类推荐请求失败 account_id=%s: %s", account_id, exc)
            raise CategoryRecommendationError("分类推荐接口请求失败，请稍后重试") from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("分类推荐处理异常 account_id=%s", account_id)
            raise CategoryRecommendationError("分类推荐接口返回异常，请稍后重试") from exc


__all__ = ["CategoryRecommendationError", "PlatformCategoryService"]
