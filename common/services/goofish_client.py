"""闲鱼 mtop 搜索客户端。

客户端只接收调用方提供的 Cookie，不会读取本机浏览器或写入隐式登录态。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from common.services.goofish_mtop import mtop_call


APP_KEY = "34839810"
SEARCH_API = "mtop.taobao.idlemtopsearch.pc.search"
ACCOUNT_ITEMS_API = "mtop.idle.web.xyh.item.list"
SOLD_ORDERS_API = "mtop.taobao.idle.trade.merchant.sold.get"
ORDER_DETAIL_API = "mtop.idle.web.trade.order.detail"
MTOP_URL = "https://h5api.m.goofish.com/h5/{api}/1.0/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.goofish.com",
    "Referer": "https://www.goofish.com/",
}


def parse_cookie_string(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in value.split(";"):
        key, separator, item = part.strip().partition("=")
        if separator and key:
            result[key] = item
    return result


def _sign(timestamp: str, token: str, data: str) -> str:
    return hashlib.md5(f"{token}&{timestamp}&{APP_KEY}&{data}".encode()).hexdigest()


def _normalize_price(value: Any) -> str | None:
    if isinstance(value, list):
        value = "".join(str(item.get("text", "")) for item in value if isinstance(item, dict))
    if value in (None, "", "0", 0, "无"):
        return None
    return str(value).replace("¥", "").replace("￥", "").strip() or None


def _normalize_amount(value: Any) -> float | None:
    """把闲鱼金额字段转成数据库可接受的数值。"""
    value = _normalize_price(value)
    if value is None:
        return None
    try:
        return float(value.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _integer_value(value: Any) -> int | None:
    """把平台库存字段安全转换为非负整数；没有字段时返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return max(number, 0)


def _first_inventory_value(source: dict[str, Any]) -> int | None:
    """读取不同版本闲鱼商品卡片可能使用的库存字段。"""
    for key in (
        "quantity",
        "stock",
        "inventory",
        "availableQuantity",
        "availableStock",
        "remainQuantity",
        "remainStock",
    ):
        if key in source:
            value = _integer_value(source.get(key))
            if value is not None:
                return value
    return None


def _platform_item_status(status_code: Any, status_text: Any, *, default: str = "on_sale") -> str:
    """将闲鱼详情里的状态统一为本地商品状态。"""
    text = str(status_text or "").strip().lower()
    if any(word in text for word in ("已下架", "卖掉", "已售罄", "售罄", "下架")):
        return "off_sale"
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        code = None
    # 详情接口实测：1=卖掉了，-2=已下架。
    if code in {1, -2}:
        return "off_sale"
    if any(word in text for word in ("在售", "出售中", "可购买")):
        return "on_sale"
    return default


def normalize_search_row(row: dict[str, Any]) -> dict[str, Any]:
    main = ((row.get("data") or {}).get("item") or {}) if isinstance(row, dict) else {}
    main = main.get("main") or main
    extra = main.get("exContent") or {}
    args = (main.get("clickParam") or {}).get("args") or {}
    item_id = extra.get("itemId") or args.get("item_id") or args.get("id")
    title = extra.get("title") or args.get("title") or ""
    return {
        "item_id": str(item_id) if item_id else None,
        "title": str(title).strip(),
        "price": _normalize_price(extra.get("price") or args.get("displayPrice") or args.get("price")),
        "seller": extra.get("userNickName") or extra.get("userNick"),
        "area": extra.get("area"),
        "url": f"https://www.goofish.com/item?id={item_id}" if item_id else None,
        "raw": row,
    }


def normalize_account_item(card: dict[str, Any]) -> dict[str, Any]:
    """解析 mtop.idle.web.xyh.item.list 返回的商品卡片。"""
    card_data = card.get("cardData") or {} if isinstance(card, dict) else {}
    item_id = card_data.get("id") or card_data.get("itemId")
    price_info = card_data.get("priceInfo") or {}
    pic_info = card_data.get("picInfo") or {}
    description = card_data.get("description") or card_data.get("desc")
    quantity = _first_inventory_value(card_data)
    status_code = card_data.get("itemStatus")
    status = _platform_item_status(status_code, card_data.get("itemStatusStr"))
    return {
        "external_id": str(item_id) if item_id else None,
        "title": str(card_data.get("title") or "未命名商品").strip(),
        "description": str(description).strip() if description else None,
        "price": _normalize_amount(price_info.get("price") or card_data.get("price")),
        "stock": quantity if quantity is not None else 0,
        "stock_known": quantity is not None,
        "images": pic_info,
        "status": status,
        "category_id": card_data.get("categoryId"),
        "item_status": status_code,
        "item_status_text": card_data.get("itemStatusStr"),
        "detail_url": card_data.get("detailUrl"),
        "raw": card,
    }


class GoofishClient:
    def __init__(self, cookie_value: str, proxy: str | None = None) -> None:
        self.cookie_value = cookie_value or ""
        self.cookies = parse_cookie_string(cookie_value)
        self.proxy = proxy or None

    async def search(self, keyword: str, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        if not self.cookies:
            raise ValueError("账号 Cookie 为空，请先扫码登录")
        data = json.dumps({"pageNumber": page, "keyword": keyword, "rowsPerPage": page_size, "searchReqFromPage": "pcSearch"}, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(__import__("time").time_ns() // 1_000_000)
        token_cookie = self.cookies.get("_m_h5_tk", "")
        token = token_cookie.split("_", 1)[0] if "_" in token_cookie else ""
        params = {
            "jsv": "2.7.2", "appKey": APP_KEY, "t": timestamp,
            "sign": _sign(timestamp, token, data), "v": "1.0", "type": "originaljson",
            "accountSite": "xianyu", "dataType": "json", "timeout": "20000",
            "api": SEARCH_API, "sessionOption": "AutoLoginOnly", "spm_cnt": "a21ybx.home.0.0",
        }
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, proxy=self.proxy) as client:
            response = await client.post(MTOP_URL.format(api=SEARCH_API), params=params, data={"data": data}, headers=HEADERS, cookies=self.cookies)
        response.raise_for_status()
        payload = response.json()
        ret = payload.get("ret", [])
        if any("RGV587" in str(item) or ("FAIL" in str(item) and "SESSION" in str(item)) for item in ret):
            raise RuntimeError("闲鱼接口触发风控或登录态失效，请稍后重试或重新扫码")
        result_data = payload.get("data") or {}
        rows = result_data.get("resultList") or []
        return {
            "items": [normalize_search_row(row) for row in rows if isinstance(row, dict)],
            "total": int(result_data.get("totalResults") or result_data.get("total") or len(rows)),
            "raw": payload,
        }

    async def list_account_items(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
        group_name: str = "在售",
        group_id: str = "58877261",
    ) -> dict[str, Any]:
        """获取登录账号自己的商品列表。

        该接口来自闲鱼卖家工作台的商品列表请求，与公开关键词搜索不同，
        必须使用当前账号的登录 Cookie 和平台用户 ID。
        """
        if not self.cookies:
            raise ValueError("账号 Cookie 为空，请先扫码登录")
        if not str(user_id or "").strip():
            raise ValueError("缺少闲鱼平台账号 ID，无法同步商品")
        data = json.dumps(
            {
                "needGroupInfo": False,
                "pageNumber": page,
                "pageSize": page_size,
                "groupName": group_name,
                "groupId": group_id,
                "defaultGroup": True,
                "userId": str(user_id).strip(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        timestamp = str(__import__("time").time_ns() // 1_000_000)
        token_cookie = self.cookies.get("_m_h5_tk", "")
        token = token_cookie.split("_", 1)[0] if "_" in token_cookie else ""
        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": timestamp,
            "sign": _sign(timestamp, token, data),
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": ACCOUNT_ITEMS_API,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.im.0.0",
            "spm_pre": "a21ybx.collection.menu.1.272b5141NafCNK",
        }
        async with httpx.AsyncClient(
            timeout=25,
            follow_redirects=True,
            proxy=self.proxy,
        ) as client:
            response = await client.post(
                MTOP_URL.format(api=ACCOUNT_ITEMS_API),
                params=params,
                data={"data": data},
                headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                cookies=self.cookies,
            )
        response.raise_for_status()
        payload = response.json()
        ret = payload.get("ret", [])
        if any(
            "RGV587" in str(item)
            or "TOKEN" in str(item).upper()
            or "SESSION" in str(item).upper()
            for item in ret
        ):
            raise RuntimeError("闲鱼登录态已失效或触发风控，请重新扫码登录")
        if ret and not any(str(item).startswith("SUCCESS") for item in ret):
            raise RuntimeError(f"闲鱼商品接口返回异常：{ret[0]}")
        result_data = payload.get("data") or {}
        cards = result_data.get("cardList") or []
        items = [
            normalize_account_item(card)
            for card in cards
            if isinstance(card, dict) and normalize_account_item(card).get("external_id")
        ]
        return {
            "items": items,
            "total": int(result_data.get("total") or result_data.get("totalCount") or len(items)),
            "page": page,
            "page_size": page_size,
            "raw": payload,
        }

    async def get_item_inventory(self, account_id: str, item_id: str) -> dict[str, Any]:
        """从商品详情接口补读线上库存和真实上下架状态。

        ``mtop.idle.web.xyh.item.list`` 的商品卡片经常不返回库存，尤其是
        普通卖家账号；详情接口的 ``itemDO.quantity`` 才是当前线上可售数量。
        """
        item_id = str(item_id or "").strip()
        if not item_id:
            return {"success": False, "error": "商品 ID 为空", "cookies_str": self.cookie_value}
        result = await mtop_call(
            api="mtop.taobao.idle.pc.detail",
            data={"itemId": item_id},
            cookies_str=self.cookie_value,
            account_id=str(account_id or "").strip(),
            proxy=self.proxy,
            extra_params={"spm_cnt": "a21ybx.item.0.0"},
        )
        self.cookie_value = str(result.get("cookies_str") or self.cookie_value)
        self.cookies = parse_cookie_string(self.cookie_value)
        if not result.get("success"):
            return {
                "success": False,
                "account_invalid": bool(result.get("account_invalid")),
                "error": result.get("error") or "商品详情获取失败",
                "cookies_str": self.cookie_value,
            }
        data = ((result.get("res") or {}).get("data") or {})
        if not isinstance(data, dict):
            return {"success": False, "account_invalid": False, "error": "商品详情返回格式异常", "cookies_str": self.cookie_value}
        item = data.get("itemDO") or data.get("item") or {}
        if not isinstance(item, dict):
            item = {}
        quantity_raw = item.get("quantity")
        quantity = _integer_value(quantity_raw)
        status_code = item.get("itemStatus")
        status_text = item.get("itemStatusStr") or item.get("statusStr")
        status = _platform_item_status(status_code, status_text)
        if status == "off_sale" and quantity is None:
            quantity = 0
        return {
            "success": True,
            "stock": quantity,
            "stock_known": quantity is not None,
            "status": status,
            "status_code": status_code,
            "status_text": str(status_text or "").strip(),
            "cookies_str": self.cookie_value,
        }

    async def list_sold_orders(
        self,
        page: int = 1,
        page_size: int = 30,
        query_code: str = "ALL",
    ) -> dict[str, Any]:
        """获取当前卖家账号的已售订单列表。"""
        if not self.cookies:
            raise ValueError("账号 Cookie 为空，请先扫码登录")
        data = json.dumps(
            {
                "pageNumber": page,
                "rowsPerPage": page_size,
                "orderIds": "",
                "queryCode": query_code,
                "orderSearchParam": "{}",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        timestamp = str(__import__("time").time_ns() // 1_000_000)
        token_cookie = self.cookies.get("_m_h5_tk", "")
        token = token_cookie.split("_", 1)[0] if "_" in token_cookie else ""
        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": timestamp,
            "sign": _sign(timestamp, token, data),
            "v": "1.0",
            "type": "json",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": SOLD_ORDERS_API,
            "valueType": "string",
            "sessionOption": "AutoLoginOnly",
        }
        headers = {
            **HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://seller.goofish.com/",
            "idle_site_biz_code": "COMMONPRO",
        }
        async with httpx.AsyncClient(
            timeout=25,
            follow_redirects=True,
            proxy=self.proxy,
        ) as client:
            response = await client.post(
                MTOP_URL.format(api=SOLD_ORDERS_API),
                params=params,
                data={"data": data},
                headers=headers,
                cookies=self.cookies,
            )
        response.raise_for_status()
        payload = response.json()
        ret = payload.get("ret", [])
        if any(
            "RGV587" in str(item)
            or "TOKEN" in str(item).upper()
            or "SESSION" in str(item).upper()
            for item in ret
        ):
            raise RuntimeError("闲鱼登录态已失效或触发风控，请重新扫码登录")
        if ret and not any(str(item).startswith("SUCCESS") for item in ret):
            raise RuntimeError(f"闲鱼订单接口返回异常：{ret[0]}")
        module = ((payload.get("data") or {}).get("module") or {})
        items = module.get("items") or []
        return {
            "items": [item for item in items if isinstance(item, dict)],
            "total": int(module.get("totalCount") or len(items)),
            "has_more": str(module.get("nextPage", "false")).lower() == "true",
            "page": page,
            "page_size": page_size,
            "raw": payload,
        }

    async def get_order_detail(self, order_no: str) -> dict[str, Any]:
        """读取单笔订单详情，补齐实时付款事件缺失的数量和规格。"""
        if not self.cookies:
            raise ValueError("账号 Cookie 为空，请先扫码登录")
        order_no = str(order_no or "").strip()
        if not order_no:
            raise ValueError("订单号为空")
        data = json.dumps({"tid": order_no}, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(__import__("time").time_ns() // 1_000_000)
        token_cookie = self.cookies.get("_m_h5_tk", "")
        token = token_cookie.split("_", 1)[0] if "_" in token_cookie else ""
        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": timestamp,
            "sign": _sign(timestamp, token, data),
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": ORDER_DETAIL_API,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.order-detail.0.0",
        }
        headers = {**HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            proxy=self.proxy,
        ) as client:
            response = await client.post(
                MTOP_URL.format(api=ORDER_DETAIL_API),
                params=params,
                data={"data": data},
                headers=headers,
                cookies=self.cookies,
            )
        response.raise_for_status()
        payload = response.json()
        ret = payload.get("ret", [])
        if ret and not any(str(item).startswith("SUCCESS") for item in ret):
            raise RuntimeError(f"闲鱼订单详情接口返回异常：{ret[0]}")

        result: dict[str, Any] = {
            "order_no": order_no,
            "quantity": 1,
            "amount": None,
            "spec_name": "",
            "spec_value": "",
            "status": "",
        }
        components = ((payload.get("data") or {}).get("components") or [])
        if not isinstance(components, list):
            components = []
        for component in components:
            if not isinstance(component, dict):
                continue
            render = str(component.get("render") or "")
            value = component.get("data") or {}
            if render != "orderInfoVO" or not isinstance(value, dict):
                continue
            item_info = value.get("itemInfo") or {}
            if not isinstance(item_info, dict):
                continue
            try:
                result["quantity"] = max(1, int(item_info.get("buyAmount") or 1))
            except (TypeError, ValueError):
                result["quantity"] = 1
            result["amount"] = _normalize_amount(item_info.get("price"))
            sku = str(item_info.get("skuInfo") or "").strip()
            if ":" in sku:
                result["spec_name"], result["spec_value"] = [part.strip() for part in sku.split(":", 1)]
            elif sku:
                result["spec_value"] = sku
            result["status"] = str(value.get("orderStatus") or value.get("status") or "")
            break
        return result
