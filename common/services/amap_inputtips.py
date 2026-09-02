"""商品发布的宝贝所在地输入提示。"""
from __future__ import annotations

from typing import Any

import httpx

from common.config import settings


AMAP_URL = "https://restapi.amap.com/v3/assistant/inputtips"


class AmapInputTipsError(RuntimeError):
    pass


def _text(value: Any) -> str:
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


async def search_input_tips(keywords: str, city: str = "全国") -> dict[str, Any]:
    normalized = keywords.strip()
    if not normalized:
        raise AmapInputTipsError("请输入所在地关键词")
    if not settings.amap_web_key:
        raise AmapInputTipsError("未配置高德地图 Key，暂时无法搜索宝贝所在地")
    params = {
        "s": "rsv3",
        "key": settings.amap_web_key,
        "platform": "JS",
        "logversion": "2.0",
        "sdkversion": "2.0",
        "appname": "https://seller.goofish.com",
        "keywords": normalized,
        "city": city.strip() or "全国",
        "citylimit": "false",
        "datatype": "all",
    }
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(
                AMAP_URL,
                params=params,
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Origin": "https://seller.goofish.com",
                    "Referer": "https://seller.goofish.com/",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                },
            )
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AmapInputTipsError("所在地搜索接口请求失败，请稍后重试") from exc
    if not isinstance(body, dict) or str(body.get("status")) != "1":
        raise AmapInputTipsError(f"所在地搜索失败：{_text(body.get('info')) if isinstance(body, dict) else '接口返回异常'}")
    tips = []
    for raw in body.get("tips") or []:
        if not isinstance(raw, dict) or not _text(raw.get("name")):
            continue
        name = _text(raw.get("name"))
        district = _text(raw.get("district"))
        address = _text(raw.get("address"))
        tips.append({
            "id": _text(raw.get("id")),
            "name": name,
            "district": district,
            "adcode": _text(raw.get("adcode")),
            "location": _text(raw.get("location")),
            "address": address,
            "typecode": _text(raw.get("typecode")),
            "city": _text(raw.get("city")),
            "search_keyword": name,
            "expected_text": " / ".join(part for part in (name, district, address) if part),
        })
    return {"tips": tips, "count": len(tips), "keywords": normalized}
