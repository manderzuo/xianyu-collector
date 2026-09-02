"""闲鱼商品详情接口，用于补全监控商品的卖家真实信息。"""
from __future__ import annotations

from typing import Any

from common.services.goofish_mtop import mtop_call


async def fetch_item_detail(*, cookies: str, account_id: str, item_id: str, proxy: str | None = None) -> dict[str, Any]:
    result = await mtop_call(
        api="mtop.taobao.idle.pc.detail",
        data={"itemId": str(item_id)},
        cookies_str=cookies,
        account_id=account_id,
        proxy=proxy,
        extra_params={"spm_cnt": "a21ybx.item.0.0"},
        version="1.0",
    )
    if not result.get("success"):
        return {
            "success": False,
            "account_invalid": bool(result.get("account_invalid")),
            "item_invalid": not bool(result.get("account_invalid")) and result.get("res") is not None,
            "error": result.get("error") or "商品详情获取失败",
            "cookies_str": result.get("cookies_str") or cookies,
        }
    data = (result.get("res") or {}).get("data") if isinstance(result.get("res"), dict) else {}
    data = data if isinstance(data, dict) else {}
    seller = data.get("sellerDO") or data.get("seller") or {}
    seller = seller if isinstance(seller, dict) else {}
    seller_id = seller.get("sellerId") or seller.get("userId") or seller.get("id")
    seller_nick = seller.get("nick") or seller.get("nickname") or seller.get("userNick")
    return {
        "success": bool(seller_id),
        "account_invalid": False,
        "item_invalid": not bool(seller_id),
        "seller_user_id": str(seller_id) if seller_id is not None else "",
        "seller_nick": str(seller_nick or "")[:120],
        "detail": data,
        "error": "" if seller_id else "商品详情未返回卖家ID",
        "cookies_str": result.get("cookies_str") or cookies,
    }


__all__ = ["fetch_item_detail"]
