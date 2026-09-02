"""闲鱼网页版拍下订单客户端。

只执行 render/create 拍下，不调用付款接口。
"""
from __future__ import annotations

import json
from typing import Any

from common.services.goofish_mtop import mtop_call


YHB_ONLY_MARKERS = ("FAIL_BIZ_ITEM_ONLY_YHB_BUY_APP_LIMIT", "必走验货宝", "ONLY_YHB")


class XianyuOrderClient:
    def __init__(self, account_id: str, cookies: str, proxy: str | None = None):
        self.account_id = str(account_id)
        self.cookies_str = cookies
        self.proxy = proxy

    async def _call(self, api: str, version: str, data: dict[str, Any]) -> dict[str, Any]:
        result = await mtop_call(
            api=api,
            data=data,
            cookies_str=self.cookies_str,
            account_id=self.account_id,
            proxy=self.proxy,
            version=version,
        )
        self.cookies_str = str(result.get("cookies_str") or self.cookies_str)
        return result

    async def render(self, item_id: str) -> dict[str, Any]:
        result = await self._call("mtop.taobao.idle.trade.order.render", "7.0", {"itemId": str(item_id)})
        if not result.get("success"):
            return {"success": False, "account_invalid": bool(result.get("account_invalid")), "item_buy_info": None, "error": result.get("error")}
        data = (result.get("res") or {}).get("data") if isinstance(result.get("res"), dict) else {}
        info = data.get("commonData", {}).get("itemBuyInfo") if isinstance(data, dict) and isinstance(data.get("commonData"), dict) else None
        return {"success": bool(info), "account_invalid": False, "item_buy_info": info, "error": "" if info else "下单渲染缺少 itemBuyInfo（可能商品不可买或缺少收货地址）"}

    async def create(self, item_buy_info: list[dict[str, Any]]) -> dict[str, Any]:
        result = await self._call("mtop.taobao.idle.trade.order.create", "5.0", {"params": json.dumps(item_buy_info, ensure_ascii=False, separators=(",", ":"))})
        if not result.get("success"):
            return {"success": False, "account_invalid": bool(result.get("account_invalid")), "order_id": None, "error": result.get("error")}
        data = (result.get("res") or {}).get("data") if isinstance(result.get("res"), dict) else {}
        order_id = data.get("bizOrderIdStr") or data.get("bizOrderId")
        return {"success": True, "account_invalid": False, "order_id": str(order_id) if order_id is not None else None, "error": ""}

    async def _address(self) -> dict[str, Any]:
        result = await self._call("mtop.taobao.idle.logistic.address.list.query", "1.0", {})
        if not result.get("success"):
            return {"success": False, "account_invalid": bool(result.get("account_invalid")), "address_id": None, "error": result.get("error")}
        data = (result.get("res") or {}).get("data") if isinstance(result.get("res"), dict) else {}
        inner = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
        values = inner.get("addressList") if isinstance(inner, dict) else []
        values = values if isinstance(values, list) else []
        chosen = next((item for item in values if isinstance(item, dict) and item.get("status") == 1), values[0] if values else None)
        address_id = chosen.get("addressId") if isinstance(chosen, dict) else None
        return {"success": address_id is not None, "account_invalid": False, "address_id": address_id, "error": "" if address_id is not None else "账号未配置收货地址，验货宝下单需要收货地址"}

    async def yhb(self, item_id: str) -> dict[str, Any]:
        address = await self._address()
        if not address.get("success"):
            return {"status": "account_invalid" if address.get("account_invalid") else "failed", "order_id": None, "error": address.get("error")}
        rendered = await self._call("mtop.alibaba.idle.pc.yhb.order.create.render", "1.0", {"itemId": str(item_id)})
        if rendered.get("account_invalid"):
            return {"status": "account_invalid", "order_id": None, "error": rendered.get("error")}
        render_data = (rendered.get("res") or {}).get("data") if isinstance(rendered.get("res"), dict) else {}
        render_data = render_data if isinstance(render_data, dict) else {}
        confirm = render_data.get("yhbConfirmBuy") if isinstance(render_data.get("yhbConfirmBuy"), dict) else {}
        yhb_version = int(render_data.get("yhbVersion") or 3) if str(render_data.get("yhbVersion") or "3").isdigit() else 3
        quantity = int(confirm.get("buyQuantity") or 1) if str(confirm.get("buyQuantity") or "1").isdigit() else 1
        created = await self._call("mtop.alibaba.idle.pc.yhb.order.create", "1.0", {"itemId": str(item_id), "optionalPromotionIdValueList": "[]", "buyerAddressId": address["address_id"], "buyQuantity": quantity, "channel": "web", "channelData": json.dumps({"yhbVersion": yhb_version}, separators=(",", ":"))})
        if not created.get("success"):
            return {"status": "account_invalid" if created.get("account_invalid") else "failed", "order_id": None, "error": created.get("error")}
        data = (created.get("res") or {}).get("data") if isinstance(created.get("res"), dict) else {}
        return {"status": "success", "order_id": str(data.get("bizOrderIdStr") or data.get("bizOrderId") or ""), "error": ""}

    async def place_order(self, item_id: str) -> dict[str, Any]:
        rendered = await self.render(item_id)
        if not rendered.get("success"):
            error = str(rendered.get("error") or "")
            if rendered.get("account_invalid"):
                return {"status": "account_invalid", "order_id": None, "error": error}
            if any(marker in error for marker in YHB_ONLY_MARKERS):
                return await self.yhb(item_id)
            return {"status": "failed", "order_id": None, "error": error}
        created = await self.create(rendered["item_buy_info"])
        if not created.get("success"):
            error = str(created.get("error") or "")
            if created.get("account_invalid"):
                return {"status": "account_invalid", "order_id": None, "error": error}
            if any(marker in error for marker in YHB_ONLY_MARKERS):
                return await self.yhb(item_id)
            return {"status": "failed", "order_id": None, "error": error}
        return {"status": "success", "order_id": created.get("order_id"), "error": ""}


__all__ = ["XianyuOrderClient"]
