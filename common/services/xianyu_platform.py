"""闲鱼平台动作适配器。

这里集中放置当前重写版已经从旧版协议确认过的几个写操作：图片上传、
无物流发货、免拼发货、评价和官方黑名单。所有方法都要求真实 Cookie，
并返回平台原始错误，不把本地记录当成平台成功。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from common.config import settings
from common.services.goofish_mtop import mtop_call


UPLOAD_URL = (
    "https://stream-upload.goofish.com/api/upload.api"
    "?floderId=0&appkey=xy_chat&_input_charset=utf-8"
)


class XianyuPlatformError(RuntimeError):
    """平台适配器返回的可展示错误。"""


def _result_items(result: dict[str, Any], item_ids: list[str]) -> list[dict[str, Any]]:
    """解析批量商品接口的逐项结果，并为异常响应补齐失败项。"""
    response = result.get("res") if isinstance(result.get("res"), dict) else {}
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    nested = data.get("data") if isinstance(data.get("data"), dict) else data
    values = nested.get("itemProcessResultList") if isinstance(nested, dict) else None
    parsed: list[dict[str, Any]] = []
    if isinstance(values, list):
        for value in values:
            if not isinstance(value, dict):
                continue
            external_id = str(value.get("itemId") or value.get("item_id") or "").strip()
            if external_id:
                parsed.append({
                    "item_id": external_id,
                    "success": bool(value.get("success")),
                    "message": str(value.get("msg") or value.get("message") or ""),
                })
    by_id = {item["item_id"]: item for item in parsed}
    return [by_id.get(item_id, {"item_id": item_id, "success": False, "message": "平台未返回该商品结果"}) for item_id in item_ids]


async def offline_items(*, cookies: str, account_id: str, item_ids: list[str]) -> dict[str, Any]:
    """调用闲鱼商品下架接口。

    卖家工作台的批量下架接口对服务/技能商品会返回无权限；PC 商品
    详情页实际使用的是 ``mtop.taobao.idle.item.downshelf``，这里逐个
    调用它，既兼容普通商品，也兼容服务商品。
    """
    cleaned = list(dict.fromkeys(str(item_id or "").strip() for item_id in item_ids if str(item_id or "").strip()))
    if not cleaned:
        raise XianyuPlatformError("请选择要下架的商品")
    current_cookie = cookies
    results: list[dict[str, Any]] = []
    for item_id in cleaned:
        result = await mtop_call(
            api="mtop.taobao.idle.item.downshelf",
            data={"itemId": item_id},
            cookies_str=current_cookie,
            account_id=account_id,
            origin="https://www.goofish.com",
            referer=f"https://www.goofish.com/item?id={item_id}",
            extra_params={"spm_cnt": "a21ybx.item.0.0"},
            version="2.0",
        )
        current_cookie = str(result.get("cookies_str") or current_cookie)
        response_data = (result.get("res") or {}).get("data") if isinstance(result.get("res"), dict) else {}
        platform_success = bool(response_data.get("success")) if isinstance(response_data, dict) else False
        success = bool(result.get("success")) and (
            platform_success or not isinstance(response_data, dict) or "success" not in response_data
        )
        results.append({
            "item_id": item_id,
            "success": success,
            "message": "下架成功" if success else str(result.get("error") or "闲鱼商品下架失败"),
        })
    return {
        "success": any(item["success"] for item in results),
        "results": results,
        "success_count": sum(1 for item in results if item["success"]),
        "fail_count": sum(1 for item in results if not item["success"]),
        "cookies_str": current_cookie,
    }


async def delete_items(*, cookies: str, account_id: str, item_ids: list[str]) -> dict[str, Any]:
    """逐个调用闲鱼卖家后台删除接口，返回每个商品的真实结果。"""
    cleaned = list(dict.fromkeys(str(item_id or "").strip() for item_id in item_ids if str(item_id or "").strip()))
    if not cleaned:
        raise XianyuPlatformError("请选择要删除的闲鱼商品")
    current_cookie = cookies
    results: list[dict[str, Any]] = []
    for item_id in cleaned:
        result = await mtop_call(
            api="mtop.alibaba.idle.seller.pc.item.delete",
            data={"itemId": item_id, "draftId": None},
            cookies_str=current_cookie,
            account_id=account_id,
            origin="https://seller.goofish.com",
            referer="https://seller.goofish.com/?site=COMMONPRO",
            extra_params={"needLoginPC": "true", "showErrorToast": "true", "spm_cnt": "a21107h.42829799.0.0"},
            extra_headers={"idle_site_biz_code": "COMMONPRO"},
        )
        current_cookie = str(result.get("cookies_str") or current_cookie)
        message = str(result.get("error") or ("删除成功" if result.get("success") else "删除失败"))
        results.append({"item_id": item_id, "success": bool(result.get("success")), "message": message})
    success_count = sum(1 for item in results if item["success"])
    return {
        "success": success_count > 0,
        "results": results,
        "success_count": success_count,
        "fail_count": len(results) - success_count,
        "cookies_str": current_cookie,
    }


def _ret_message(result: dict[str, Any]) -> str:
    values = result.get("ret") or []
    return str(values[0]) if values else "闲鱼接口未返回明确结果"


def _platform_error(result: dict[str, Any], fallback: str) -> XianyuPlatformError:
    return XianyuPlatformError(_ret_message(result) if result else fallback)


def _extract_image_url(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("url", "fileUrl", "file_url", "imageUrl", "image_url"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for key in ("data", "object", "result"):
            nested = _extract_image_url(value.get(key))
            if nested:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = _extract_image_url(item)
            if nested:
                return nested
    return ""


async def upload_chat_image(path: str, cookies: str) -> str:
    """把共享静态目录中的图片上传至闲鱼 CDN。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise XianyuPlatformError("本地图片文件不存在")
    content = file_path.read_bytes()
    if not content:
        raise XianyuPlatformError("图片文件为空")
    if len(content) > 10 * 1024 * 1024:
        raise XianyuPlatformError("图片大小不能超过10MB")
    filename = f"img_{file_path.stem[:32]}.jpg"
    headers = {
        "Cookie": cookies.replace("\n", "").replace("\r", ""),
        "Referer": "https://www.goofish.com/",
        "Origin": "https://www.goofish.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=30, write=30, pool=30), proxy=settings.goofish_proxy or None) as client:
            response = await client.post(
                UPLOAD_URL,
                headers=headers,
                files={"file": (filename, content, "image/jpeg")},
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise XianyuPlatformError("闲鱼图片上传返回内容不是 JSON") from exc
    except httpx.HTTPError as exc:
        raise XianyuPlatformError(f"闲鱼图片上传请求失败：{str(exc)[:300]}") from exc
    url = _extract_image_url(payload)
    if not url:
        raise XianyuPlatformError("闲鱼图片上传失败：接口未返回有效图片地址")
    return url


async def confirm_no_logistics(
    *, cookies: str, account_id: str, order_no: str, item_id: str = "", buyer_id: str = "", is_bargain: bool = False,
) -> dict[str, Any]:
    """调用旧版确认发货/免拼发货协议。"""
    if is_bargain:
        try:
            item_value: Any = int(item_id)
        except (TypeError, ValueError):
            item_value = item_id
        try:
            buyer_value: Any = int(buyer_id)
        except (TypeError, ValueError):
            buyer_value = buyer_id
        api = "mtop.idle.groupon.activity.seller.freeshipping"
        data = {"bizOrderId": str(order_no), "itemId": item_value, "buyerId": buyer_value}
    else:
        api = "mtop.taobao.idle.logistic.consign.dummy"
        data = {"orderId": str(order_no), "tradeText": "", "picList": [], "newUnconsign": True}
    result = await mtop_call(
        api=api,
        data=data,
        cookies_str=cookies,
        account_id=account_id,
        version="1.0",
    )
    if not result.get("success"):
        message = str(result.get("error") or "无物流发货失败")
        if "ORDER_ALREADY_DELIVERY" in message or "已发货成功" in message:
            return {"success": True, "already_delivered": True, "message": message, "cookies_str": result.get("cookies_str") or cookies}
        raise XianyuPlatformError(message)
    return {"success": True, "message": "无物流发货成功", "res": result.get("res"), "cookies_str": result.get("cookies_str") or cookies}


async def close_account_notice(*, cookies: str, account_id: str) -> dict[str, Any]:
    """关闭闲鱼账号消息通知。

    该动作来自旧版账号管理的真实 mtop 协议，不能只修改本地开关。
    """
    result = await mtop_call(
        api="mtop.taobao.idlemessage.pc.profile.notice.update",
        data={"oprType": 2, "appKeys": ["444e9908a51d1cb236a27862abc769c9"]},
        cookies_str=cookies,
        account_id=account_id,
        version="1.0",
    )
    if not result.get("success"):
        raise XianyuPlatformError(str(result.get("error") or "关闭账号消息通知失败"))
    data = (result.get("res") or {}).get("data") if isinstance(result.get("res"), dict) else {}
    if isinstance(data, dict) and data.get("success") is False:
        raise XianyuPlatformError("闲鱼接口返回关闭通知失败")
    return {
        "success": True,
        "message": "账号消息通知已关闭",
        "data": data if isinstance(data, dict) else {},
        "cookies_str": result.get("cookies_str") or cookies,
    }


async def request_red_flower(*, cookies: str, account_id: str, order_no: str) -> dict[str, Any]:
    """请求订单小红花，返回平台真实结果。"""
    result = await mtop_call(
        api="mtop.taobao.idlemessage.red.flower",
        data={"orderId": str(order_no), "channel": "list"},
        cookies_str=cookies,
        account_id=account_id,
        version="1.0",
    )
    if not result.get("success"):
        raise XianyuPlatformError(str(result.get("error") or "求小红花失败"))
    return {
        "success": True,
        "message": "求小红花成功",
        "res": result.get("res"),
        "cookies_str": result.get("cookies_str") or cookies,
    }


async def rate_buyer(*, cookies: str, account_id: str, order_no: str, feedback: str) -> dict[str, Any]:
    data = {"tradeId": str(order_no), "rate": 1, "feedback": feedback or "不错的买家", "createOrAppend": 0}
    result = await mtop_call(
        api="mtop.taobao.idle.rate.create",
        data=data,
        cookies_str=cookies,
        account_id=account_id,
        version="4.0",
    )
    if not result.get("success"):
        message = str(result.get("error") or "评价失败")
        if "已评价" in message or "ALREADY" in message.upper():
            return {"success": True, "already_rated": True, "message": message, "cookies_str": result.get("cookies_str") or cookies}
        raise XianyuPlatformError(message)
    return {"success": True, "message": "评价成功", "cookies_str": result.get("cookies_str") or cookies}


async def official_blacklist(*, cookies: str, account_id: str, session_id: str, action: str) -> dict[str, Any]:
    versions = {
        "query": ("mtop.taobao.idlemessage.pc.blacklist.query", "1.0"),
        "add": ("mtop.taobao.idlemessage.pc.blacklist.add", "2.0"),
        "remove": ("mtop.taobao.idlemessage.pc.blacklist.remove", "1.0"),
    }
    if action not in versions:
        raise XianyuPlatformError("官方黑名单操作无效")
    api, version = versions[action]
    result = await mtop_call(
        api=api,
        data={"sessionId": str(session_id)},
        cookies_str=cookies,
        account_id=account_id,
        version=version,
    )
    if not result.get("success"):
        raise _platform_error(result.get("res") or {}, str(result.get("error") or "官方黑名单操作失败"))
    data = result.get("res", {}).get("data") if isinstance(result.get("res"), dict) else {}
    return {"success": True, "data": data if isinstance(data, dict) else {}, "cookies_str": result.get("cookies_str") or cookies}


__all__ = [
    "XianyuPlatformError", "upload_chat_image", "confirm_no_logistics",
    "close_account_notice", "request_red_flower", "rate_buyer", "official_blacklist",
    "offline_items", "delete_items",
]
