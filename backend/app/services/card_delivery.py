# -*- coding: utf-8 -*-
"""订单卡券发货链路。

卡券配置本身保存在 ``FeatureRecord`` 中，本服务负责把它真正转换成买家
消息：匹配商品/规格、消耗批量卡密、调用外部 API、通过已建立的闲鱼 IM
发送，并用 ``CardDeliveryRecord`` 保证重复点击不会重复耗卡。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.config import settings
from common.db.session import async_session_maker
from common.models.account_contents import AccountContent
from common.models.account_cookies import AccountCookie
from common.models.accounts import Account
from common.models.card_delivery import CardDeliveryRecord
from common.models.feature_records import FeatureRecord
from common.models.orders import Order
from common.services.goofish_client import GoofishClient
from common.services.xianyu_platform import XianyuPlatformError, confirm_no_logistics
from backend.app.services.account_settings import load_account_settings


ELIGIBLE_ORDER_STATUSES = {"pending", "pending_payment", "paid", "processing", "pending_ship"}
PAYMENT_EVENT_PARTS = (
    "[我已付款，等待你发货]",
    "[已付款，待发货]",
    "我已付款，等待你发货",
    "[记得及时发货]",
    "[买家已付款]",
    "买家已付款",
    "[付款完成]",
    "付款完成",
)
logger = logging.getLogger("xr.backend.card_delivery")


def _buyer_name(value: Any) -> str | None:
    name = str(value or "").strip()
    if not name or name.startswith(("[", "【")):
        return None
    if any(marker in name for marker in ("拍下", "付款", "发货", "退款", "评价", "交易成功", "订单")):
        return None
    return name[:255]


class CardDeliveryError(RuntimeError):
    """可直接展示给用户的发货错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _json_value(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, (dict, list, int, float, bool)):
        return raw
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CardDeliveryError("card_config_invalid", "卡券 API 配置不是有效的 JSON") from exc


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _order_variables(order: Order, account: Account) -> dict[str, str]:
    payload = order.payload if isinstance(order.payload, dict) else {}
    item_detail = payload.get("item_detail") or payload.get("itemDetail") or payload
    return {
        "order_id": str(order.order_no or ""),
        "item_id": str(order.item_external_id or order.product_id or ""),
        "item_detail": _stringify(item_detail),
        "order_amount": _stringify(order.amount),
        "order_quantity": str(order.quantity or 1),
        "spec_name": str(order.spec_name or ""),
        "spec_value": str(order.spec_value or ""),
        "cookie_id": str(account.id),
        "buyer_id": str(order.buyer_id or ""),
        "item_title": str(order.item_title or ""),
        "buyer_name": str(order.buyer_nick or ""),
        "seller_name": str(account.account_name or ""),
    }


def _replace_variables(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {str(key): _replace_variables(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_variables(item, variables) for item in value]
    if isinstance(value, str):
        return re.sub(r"\{([a-zA-Z0-9_]+)\}", lambda match: variables.get(match.group(1), match.group(0)), value)
    return value


def _extract_path(value: Any, path: str) -> Any:
    current = value
    for part in [item for item in re.split(r"\.|\[|\]", path.strip()) if item]:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)] if int(part) < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


def _api_content(response: httpx.Response, response_field: str) -> str:
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text

    value = _extract_path(body, response_field) if response_field.strip() else None
    if value is None and not response_field.strip() and isinstance(body, dict):
        for key in ("data", "content", "card"):
            if body.get(key) not in (None, "", [], {}):
                candidate = body[key]
                if isinstance(candidate, dict):
                    for child_key in ("content", "card", "code", "key", "value"):
                        if candidate.get(child_key) not in (None, ""):
                            candidate = candidate[child_key]
                            break
                value = candidate
                break
    if value is None:
        value = body
    if value in (None, "", [], {}):
        raise CardDeliveryError("api_empty_content", "卡券 API 未返回有效卡券内容")
    return _stringify(value)


async def _fetch_api_content(card: FeatureRecord, order: Order, account: Account) -> str:
    config = card.payload.get("api_config") if isinstance(card.payload, dict) else None
    if not isinstance(config, dict) or not str(config.get("url") or "").strip():
        raise CardDeliveryError("card_config_invalid", "API 卡券缺少接口地址")
    variables = _order_variables(order, account)
    url = str(_replace_variables(config.get("url"), variables)).strip()
    method = str(config.get("method") or "GET").upper()
    headers = _replace_variables(_json_value(config.get("headers"), {}), variables)
    params = _replace_variables(_json_value(config.get("params"), {}), variables)
    if not isinstance(headers, dict):
        raise CardDeliveryError("card_config_invalid", "API 请求头必须是 JSON 对象")
    timeout = max(1, min(int(config.get("timeout") or 60), 120))
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            if method == "POST":
                response = await client.post(url, headers={str(k): str(v) for k, v in headers.items()}, json=params)
            else:
                response = await client.get(url, headers={str(k): str(v) for k, v in headers.items()}, params=params)
            response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        raise CardDeliveryError("api_request_failed", f"卡券 API 请求失败：{str(exc)[:300]}") from exc
    return _api_content(response, str(config.get("response_field") or ""))


def _item_candidates(order: Order) -> set[str]:
    return {
        str(value)
        for value in (order.item_external_id, order.product_id, order.payload.get("itemId") if isinstance(order.payload, dict) else None)
        if value not in (None, "", "-")
    }


async def _find_card(db: AsyncSession, order: Order, account: Account, requested_card_id: int | None = None) -> FeatureRecord:
    statement = select(FeatureRecord).where(
        FeatureRecord.owner_id == int(account.user_id),
        FeatureRecord.feature == "cards",
        FeatureRecord.status == "active",
    ).order_by(FeatureRecord.id.asc())
    if requested_card_id is not None:
        statement = statement.where(FeatureRecord.id == requested_card_id)
    cards = (await db.execute(statement)).scalars().all()
    item_candidates = _item_candidates(order)
    for card in cards:
        payload = dict(card.payload or {})
        if payload.get("enabled", True) is False:
            continue
        related = {str(value) for value in (payload.get("item_ids") or [])}
        if payload.get("item_id") not in (None, ""):
            related.add(str(payload["item_id"]))
        if not item_candidates or not related.intersection(item_candidates):
            continue
        if payload.get("is_multi_spec"):
            if str(payload.get("spec_name") or "") != str(order.spec_name or "") or str(payload.get("spec_value") or "") != str(order.spec_value or ""):
                continue
        return card
    if requested_card_id is not None:
        raise CardDeliveryError("card_not_available", "指定卡券不存在、已停用或未关联此商品/规格")
    raise CardDeliveryError("card_not_configured", "该订单没有匹配到已启用的关联卡券")


async def _item_multi_quantity(db: AsyncSession, order: Order) -> bool:
    if not order.item_external_id:
        return False
    item = (
        await db.execute(
            select(AccountContent).where(
                AccountContent.account_id == order.account_id,
                AccountContent.content_type == "product",
                AccountContent.external_id == order.item_external_id,
            )
        )
    ).scalar_one_or_none()
    return bool(item and isinstance(item.payload, dict) and item.payload.get("multi_quantity_delivery"))


async def _reserve_data_content(db: AsyncSession, card: FeatureRecord, count: int) -> list[str]:
    locked = (
        await db.execute(
            select(FeatureRecord).where(FeatureRecord.id == card.id).with_for_update()
        )
    ).scalar_one_or_none()
    if locked is None:
        raise CardDeliveryError("card_not_found", "卡券不存在")
    payload = dict(locked.payload or {})
    lines = [line.strip() for line in str(payload.get("data_content") or "").splitlines() if line.strip()]
    if len(lines) < count:
        raise CardDeliveryError("card_stock_empty", f"批量卡券库存不足，需要 {count} 条，剩余 {len(lines)} 条")
    selected, remaining = lines[:count], lines[count:]
    payload["data_content"] = "\n".join(remaining)
    locked.payload = payload
    await db.commit()
    card.payload = payload
    return selected


async def _restore_data_content(db: AsyncSession, card_id: int, values: list[str]) -> None:
    if not values:
        return
    locked = (
        await db.execute(select(FeatureRecord).where(FeatureRecord.id == card_id).with_for_update())
    ).scalar_one_or_none()
    if locked is None:
        return
    payload = dict(locked.payload or {})
    current = [line.strip() for line in str(payload.get("data_content") or "").splitlines() if line.strip()]
    payload["data_content"] = "\n".join(values + current)
    locked.payload = payload
    await db.commit()


async def _send_to_buyer(account: Account, buyer_id: str, cid: str | None, text: str) -> str:
    endpoint = "send-text" if cid else "send-to-buyer"
    body = {"cid": cid or "", "to_user_id": buyer_id, "text": text}
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            response = await client.post(
                f"{settings.websocket_service_url.rstrip('/')}/internal/chat/{account.id}/{endpoint}",
                json=body,
            )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not response.is_success or not payload.get("success"):
            raise CardDeliveryError("message_send_failed", str(payload.get("message") or "闲鱼消息发送失败")[:500])
        data = payload.get("data") or {}
        return str(data.get("messageId") or data.get("message_id") or "")
    except httpx.HTTPError as exc:
        raise CardDeliveryError("message_service_unavailable", f"闲鱼消息服务不可用：{str(exc)[:300]}") from exc


def _card_image_paths(payload: dict[str, Any]) -> list[str]:
    values = payload.get("image_urls")
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except (TypeError, ValueError, json.JSONDecodeError):
            values = [values]
    if not isinstance(values, list):
        values = [payload.get("image_url")] if payload.get("image_url") else []
    return [str(value).strip() for value in values if str(value or "").strip()]


async def _send_image_to_buyer(account: Account, buyer_id: str, cid: str | None, image_value: str) -> tuple[str, str]:
    """把卡券图片转成 CDN 地址后通过 websocket 发送。"""
    from common.services.xianyu_platform import upload_chat_image

    value = str(image_value or "").strip()
    if not value:
        raise CardDeliveryError("card_content_empty", "图片卡券没有图片地址")
    if value.startswith("http://") or value.startswith("https://"):
        image_url = value
    else:
        static_root = Path(settings.static_dir).resolve()
        if value.startswith("/static/"):
            local_path = static_root / value.removeprefix("/static/")
        else:
            local_path = Path(value).resolve()
        if static_root not in local_path.parents:
            raise CardDeliveryError("card_image_invalid", "卡券图片不在系统静态目录内")
        try:
            image_url = await upload_chat_image(str(local_path), account.cookie or "")
        except Exception as exc:
            raise CardDeliveryError("image_upload_failed", f"闲鱼图片上传失败：{str(exc)[:400]}") from exc
    endpoint = f"{settings.websocket_service_url.rstrip('/')}/internal/chat/{account.id}/send-image-url"
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(endpoint, json={"cid": cid or "", "to_user_id": buyer_id, "image_url": image_url})
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CardDeliveryError("message_service_unavailable", f"图片消息服务不可用：{str(exc)[:300]}") from exc
    if not response.is_success or not result.get("success"):
        raise CardDeliveryError("message_send_failed", str(result.get("message") or result.get("detail") or "闲鱼图片消息发送失败")[:500])
    data = result.get("data") or {}
    return image_url, str(data.get("messageId") or data.get("message_id") or "")


async def _resolve_cid(account: Account, buyer_id: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            response = await client.post(
                f"{settings.websocket_service_url.rstrip('/')}/internal/chat/{account.id}/find-conversation",
                json={"buyer_id": buyer_id},
            )
        payload = response.json()
        if response.is_success and payload.get("success"):
            return str((payload.get("data") or {}).get("cid") or "") or None
        message = payload.get("message") or "找不到买家的闲鱼会话"
        raise CardDeliveryError("conversation_not_found", message)
    except CardDeliveryError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise CardDeliveryError("message_service_unavailable", f"闲鱼会话服务不可用：{str(exc)[:300]}") from exc


async def _record_failure(
    db: AsyncSession,
    order: Order,
    account: Account,
    error: CardDeliveryError,
    card: FeatureRecord | None = None,
    delivery_content: str | None = None,
    source: str = "own",
) -> dict[str, Any]:
    order.delivery_method = "scheduled" if source == "scheduled" else ("auto" if source == "auto" else "manual")
    order.delivery_send_status = "failed"
    order.delivery_fail_reason = error.message
    order.delivery_send_fail_reason = error.message
    record = CardDeliveryRecord(
        owner_id=int(account.user_id), order_id=order.id, order_no=order.order_no,
        card_id=card.id if card else None, card_name=str((card.payload or {}).get("name") or "") if card else None,
        source=source, status="failed", delivery_content=delivery_content,
        error_code=error.code, error_message=error.message, attempt_count=1,
    )
    db.add(record)
    await db.commit()
    return {"status": "failed", "code": error.code, "message": error.message, "order_no": order.order_no}


async def _confirm_platform_delivery(
    db: AsyncSession,
    account: Account,
    order: Order,
) -> dict[str, Any]:
    """调用闲鱼无物流发货接口，统一处理 Cookie 更新和平台错误。"""
    payload = order.payload if isinstance(order.payload, dict) else {}
    try:
        result = await confirm_no_logistics(
            cookies=account.cookie or "",
            account_id=str(account.goofish_id or account.id),
            order_no=order.order_no,
            item_id=str(order.item_external_id or payload.get("itemId") or ""),
            buyer_id=str(order.buyer_id or ""),
            is_bargain=bool(payload.get("is_bargain") or payload.get("isBargain")),
        )
    except XianyuPlatformError as exc:
        return {"success": False, "message": str(exc), "code": "platform_delivery_failed"}
    refreshed_cookie = str(result.get("cookies_str") or "").strip()
    if refreshed_cookie and refreshed_cookie != (account.cookie or "").strip():
        account.cookie = refreshed_cookie
        db.add(AccountCookie(account_id=account.id, cookie_value=refreshed_cookie, status="active"))
    return {
        "success": True,
        "already_delivered": bool(result.get("already_delivered")),
        "message": str(result.get("message") or "闲鱼平台已确认发货"),
    }


async def deliver_order(
    db: AsyncSession,
    order: Order,
    account: Account,
    *,
    source: str = "manual",
    requested_card_id: int | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """发送一笔订单卡券，成功返回可供前端展示的结果。"""
    # 手动按钮、订单同步和调度器可能同时命中同一笔订单。先锁订单并
    # 重新读取最新状态，避免文本/API 卡券在并发下重复发送。
    locked_order = (
        await db.execute(select(Order).where(Order.id == order.id).with_for_update())
    ).scalar_one_or_none()
    if locked_order is None:
        raise CardDeliveryError("order_not_found", "订单不存在")
    order = locked_order
    if order.delivery_send_status == "success" or order.card_only_delivered:
        return {"status": "sent", "already_sent": True, "order_no": order.order_no, "message": "该订单卡券已经发送过"}
    if order.delivery_send_status == "sending":
        return {"status": "sending", "order_no": order.order_no, "message": "该订单正在发货，请勿重复点击"}
    if order.status not in ELIGIBLE_ORDER_STATUSES:
        return await _record_failure(db, order, account, CardDeliveryError("order_not_shippable", "当前订单状态不可发货"), source=source)
    if not order.buyer_id:
        return await _record_failure(db, order, account, CardDeliveryError("buyer_missing", "订单缺少买家ID，无法找到聊天会话"), source=source)

    last = (
        await db.execute(
            select(CardDeliveryRecord).where(CardDeliveryRecord.order_id == order.id).order_by(CardDeliveryRecord.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if last and last.status == "sending":
        return {"status": "sending", "order_no": order.order_no, "message": "该订单正在发货，请勿重复点击"}
    card: FeatureRecord | None = None
    reserved: list[str] = []
    record: CardDeliveryRecord | None = None
    try:
        account_settings = await load_account_settings(db, int(account.user_id), int(account.id))
        if account_settings.get("delivery_disabled"):
            raise CardDeliveryError("delivery_disabled", str(account_settings.get("delivery_disabled_reason") or "该账号已暂停发货"))
        card = await _find_card(db, order, account, requested_card_id)
        card_payload = dict(card.payload or {})
        card_type = str(card_payload.get("type") or "text")
        if card_type == "image" and not _card_image_paths(card_payload):
            raise CardDeliveryError("card_content_empty", "图片卡券没有配置图片")
        count = int(order.quantity or 1) if await _item_multi_quantity(db, order) else 1
        only_send_card = bool(account_settings.get("only_send_card"))
        auto_confirm = bool(account_settings.get("auto_confirm"))
        send_before_confirm = bool(account_settings.get("send_before_confirm"))
        confirm_before_send = bool(account_settings.get("confirm_before_send"))
        platform_sync = "skipped_only_send_card" if only_send_card else "not_enabled"
        platform_error = ""

        # 旧版正常发货链路会先在闲鱼确认无物流发货，再向买家发送卡券；
        # “只发卡券”和“卡券发送成功再确认发货”保留原有语义。手动发货
        # 也沿用账号开关，避免只写本地订单状态而不改变闲鱼订单。
        if auto_confirm and not only_send_card and not send_before_confirm:
            platform_result = await _confirm_platform_delivery(db, account, order)
            if platform_result.get("success"):
                platform_sync = "success"
                order.status = "shipped"
            else:
                platform_error = str(platform_result.get("message") or "闲鱼平台确认发货失败")
                platform_sync = "failed"
                if confirm_before_send:
                    raise CardDeliveryError("platform_delivery_failed", platform_error)

        # 先持久化发送中状态，再扣减批量卡密或调用外部 API。这样订单同步、
        # 调度器和手动按钮并发命中时，后续调用会立即返回而不会重复发货。
        order.delivery_method = "scheduled" if source == "scheduled" else ("auto" if source == "auto" else "manual")
        order.delivery_send_status = "sending"
        order.delivery_send_fail_reason = None
        await db.commit()

        if card_type == "data":
            reserved = await _reserve_data_content(db, card, count)
            delivery_content = "\n".join(reserved)
        elif card_type == "text":
            delivery_content = str(card_payload.get("text_content") or "").strip()
            if not delivery_content:
                raise CardDeliveryError("card_content_empty", "固定文字卡券内容为空")
        elif card_type == "api":
            delivery_content = ""
        else:
            raise CardDeliveryError("card_type_invalid", f"不支持的卡券类型：{card_type}")

        record = CardDeliveryRecord(
            owner_id=int(account.user_id), order_id=order.id, order_no=order.order_no,
            card_id=card.id, card_name=str(card_payload.get("name") or ""), source=source,
            status="sending", delivery_content=delivery_content or None, attempt_count=(last.attempt_count + 1 if last else 1),
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)

        if card_type == "api":
            delivery_content = await _fetch_api_content(card, order, account)
            record.delivery_content = delivery_content
            await db.commit()
        delay = max(0, min(int(card_payload.get("delay_seconds") or 0), 3600))
        if delay:
            await asyncio.sleep(delay)
        cid = conversation_id or await _resolve_cid(account, str(order.buyer_id))
        message_ids: list[str] = []
        if card_type == "image":
            image_urls: list[str] = []
            for image_value in _card_image_paths(card_payload):
                image_url, message_id = await _send_image_to_buyer(account, str(order.buyer_id), cid, image_value)
                image_urls.append(image_url)
                message_ids.append(message_id)
                await asyncio.sleep(0.5)
            variables = _order_variables(order, account)
            template = str(card_payload.get("description") or "").strip()
            if template:
                message_text = _replace_variables(template, {**variables, "DELIVERY_CONTENT": "\n".join(image_urls)})
                for segment in [part.strip() for part in re.split(r"######", str(message_text)) if part.strip()]:
                    message_ids.append(await _send_to_buyer(account, str(order.buyer_id), cid, segment))
                    await asyncio.sleep(0.5)
            delivery_content = "\n".join(image_urls)
        else:
            variables = _order_variables(order, account)
            template = str(card_payload.get("description") or "").strip()
            message_text = _replace_variables(template, {**variables, "DELIVERY_CONTENT": delivery_content}) if template else delivery_content
            segments = [part.strip() for part in re.split(r"######", str(message_text)) if part.strip()]
            if not segments:
                raise CardDeliveryError("card_content_empty", "卡券最终发送内容为空")
            for index, segment in enumerate(segments):
                message_ids.append(await _send_to_buyer(account, str(order.buyer_id), cid, segment))
                if index < len(segments) - 1:
                    await asyncio.sleep(0.5)

        order.delivery_method = "scheduled" if source == "scheduled" else ("auto" if source == "auto" else "manual")
        order.delivery_content = str(delivery_content)
        order.delivery_send_status = "success"
        order.delivery_fail_reason = None
        order.delivery_send_fail_reason = None
        order.delivered_at = datetime.utcnow()
        if only_send_card:
            order.card_only_delivered = True
        elif auto_confirm and send_before_confirm:
            platform_result = await _confirm_platform_delivery(db, account, order)
            if platform_result.get("success"):
                platform_sync = "success"
                order.status = "shipped"
            else:
                platform_sync = "failed"
                platform_error = str(platform_result.get("message") or "闲鱼平台确认发货失败")
        payload = dict(card.payload or {})
        payload["delivery_count"] = int(payload.get("delivery_count") or 0) + 1
        card.payload = payload
        record.status = "sent"
        record.message_ids = message_ids
        record.sent_at = datetime.utcnow()
        record.error_code = None
        record.error_message = None
        await db.commit()
        return {
            "status": "sent", "order_no": order.order_no, "card_id": card.id,
            "card_name": str(card_payload.get("name") or ""), "card_type": card_type,
            "message_ids": message_ids, "platform_sync": platform_sync,
            "platform_error": platform_error or None,
            "message": "卡券已发送到闲鱼会话，平台订单已确认发货" if platform_sync == "success" else (
                "卡券已发送到闲鱼会话（只发卡券，未确认平台发货）" if platform_sync == "skipped_only_send_card" else (
                    f"卡券已发送到闲鱼会话，但平台发货确认失败：{platform_error}" if platform_error else "卡券已发送到闲鱼会话，平台发货未启用"
                )
            ),
        }
    except CardDeliveryError as exc:
        if reserved and card:
            await _restore_data_content(db, card.id, reserved)
        if record is not None:
            record.status = "failed"
            record.error_code = exc.code
            record.error_message = exc.message
            await db.commit()
            order.delivery_method = "scheduled" if source == "scheduled" else ("auto" if source == "auto" else "manual")
            order.delivery_send_status = "failed"
            order.delivery_fail_reason = exc.message
            order.delivery_send_fail_reason = exc.message
            await db.commit()
            return {"status": "failed", "code": exc.code, "message": exc.message, "order_no": order.order_no}
        return await _record_failure(db, order, account, exc, card=card, source=source)
    except Exception as exc:
        error = CardDeliveryError("delivery_failed", f"卡券发货失败：{str(exc)[:500]}")
        if reserved and card:
            await _restore_data_content(db, card.id, reserved)
        if record is not None:
            record.status = "failed"
            record.error_code = error.code
            record.error_message = error.message
            await db.commit()
            order.delivery_method = "scheduled" if source == "scheduled" else ("auto" if source == "auto" else "manual")
            order.delivery_send_status = "failed"
            order.delivery_fail_reason = error.message
            order.delivery_send_fail_reason = error.message
            await db.commit()
            return {"status": "failed", "code": error.code, "message": error.message, "order_no": order.order_no}
        return await _record_failure(db, order, account, error, card=card, source=source)


async def auto_deliver_live_event(
    account_id: int,
    conversation_id: str,
    message: dict[str, Any],
) -> dict[str, Any]:
    """处理闲鱼实时的“买家已付款”提醒并立即发卡。

    卖家订单列表接口在部分账号上会返回 PERMISSION_EXCEPTION；实时 IM 的
    付款卡片仍包含订单号，因此不能把自动发货完全绑在订单列表同步上。
    这里以订单号幂等创建本地订单，再复用同一套卡券匹配、库存锁定和消息发送链路。
    """
    if bool(message.get("isSelf")):
        return {"status": "skipped", "reason": "self_message"}
    text = str(message.get("text") or "").strip()
    if not any(part in text for part in PAYMENT_EVENT_PARTS):
        return {"status": "skipped", "reason": "not_payment_event"}

    order_no = str(
        message.get("orderId")
        or message.get("order_id")
        or message.get("bizOrderId")
        or message.get("biz_order_id")
        or message.get("orderNo")
        or message.get("order_no")
        or ""
    ).strip()
    if not order_no:
        logger.warning("账号 %s 收到付款提醒，但实时消息中未提取到订单号", account_id)
        return {"status": "skipped", "reason": "order_id_missing"}

    try:
        async with async_session_maker() as db:
            account = (
                await db.execute(select(Account).where(Account.id == int(account_id)))
            ).scalar_one_or_none()
            if account is None:
                return {"status": "failed", "code": "account_not_found", "message": "账号不存在"}
            if not (account.cookie or "").strip():
                return {"status": "failed", "code": "account_invalid", "message": "账号没有有效 Cookie"}

            item_external_id = str(
                message.get("itemId") or message.get("item_id") or ""
            ).strip() or None
            buyer_id = str(message.get("senderId") or "").strip() or None
            buyer_nick = _buyer_name(message.get("senderName"))
            item = None
            if item_external_id:
                item = (
                    await db.execute(
                        select(AccountContent).where(
                            AccountContent.account_id == account.id,
                            AccountContent.content_type == "product",
                            AccountContent.external_id == item_external_id,
                        )
                    )
                ).scalar_one_or_none()

            # 实时付款卡片通常不携带购买数量/规格；订单详情接口成功时补齐，
            # 接口受限时仍使用事件中的商品和买家信息继续走单卡默认发货。
            order_detail: dict[str, Any] = {}
            try:
                order_detail = await GoofishClient(account.cookie or "", account.proxy).get_order_detail(order_no)
            except Exception as exc:
                logger.info("账号 %s 订单 %s 详情读取失败，使用实时事件默认字段：%s", account_id, order_no, str(exc)[:200])

            order = (
                await db.execute(select(Order).where(Order.order_no == order_no))
            ).scalar_one_or_none()
            safe_event = {
                key: message.get(key)
                for key in ("messageId", "itemId", "orderId", "senderId", "senderName", "text", "time", "type")
                if message.get(key) not in (None, "")
            }
            if order is None:
                order = Order(
                    account_id=account.id,
                    order_no=order_no[:64],
                    buyer_id=buyer_id,
                    buyer_nick=buyer_nick,
                    product_id=item.id if item else None,
                    item_external_id=item_external_id,
                    item_title=str(item.title or "")[:255] if item and item.title else None,
                    quantity=max(1, int(order_detail.get("quantity") or 1)),
                    spec_name=str(order_detail.get("spec_name") or "")[:128] or None,
                    spec_value=str(order_detail.get("spec_value") or "")[:255] or None,
                    amount=float(order_detail.get("amount") or 0.0),
                    status="pending_ship",
                    payload={"live_event": safe_event},
                )
                db.add(order)
                await db.flush()
            else:
                if order.account_id != account.id:
                    logger.warning("实时付款提醒订单 %s 与账号 %s 不匹配", order_no, account_id)
                    return {"status": "failed", "code": "order_account_mismatch", "message": "订单所属账号不匹配"}
                order.buyer_id = order.buyer_id or buyer_id
                order.buyer_nick = order.buyer_nick or buyer_nick
                order.item_external_id = order.item_external_id or item_external_id
                order.product_id = order.product_id or (item.id if item else None)
                order.item_title = order.item_title or (str(item.title or "")[:255] if item and item.title else None)
                order.quantity = max(1, int(order_detail.get("quantity") or order.quantity or 1))
                order.spec_name = order.spec_name or (str(order_detail.get("spec_name") or "")[:128] or None)
                order.spec_value = order.spec_value or (str(order_detail.get("spec_value") or "")[:255] or None)
                if not order.amount and order_detail.get("amount") is not None:
                    order.amount = float(order_detail.get("amount") or 0.0)
                if order.status in {"unknown", "pending_payment"}:
                    order.status = "pending_ship"
                current_payload = dict(order.payload or {}) if isinstance(order.payload, dict) else {}
                current_payload["live_event"] = safe_event
                order.payload = current_payload
                await db.flush()

            result = await deliver_order(
                db,
                order,
                account,
                source="auto",
                conversation_id=conversation_id,
            )
            logger.info(
                "账号 %s 实时付款提醒已处理：订单=%s，状态=%s",
                account_id,
                order_no,
                result.get("status"),
            )
            return result
    except Exception as exc:
        logger.exception("账号 %s 实时付款提醒发货异常：%s", account_id, str(exc)[:300])
        return {"status": "failed", "code": "live_delivery_failed", "message": str(exc)[:500], "order_no": order_no}


async def preview_live_event(account_id: int, message: dict[str, Any]) -> dict[str, Any]:
    """只读模拟一次实时付款事件，不创建订单、不扣库存、不发送消息。"""
    if bool(message.get("isSelf")):
        return {"status": "skipped", "reason": "self_message"}
    text = str(message.get("text") or "").strip()
    if not any(part in text for part in PAYMENT_EVENT_PARTS):
        return {"status": "skipped", "reason": "not_payment_event"}
    order_no = str(
        message.get("orderId")
        or message.get("order_id")
        or message.get("bizOrderId")
        or message.get("biz_order_id")
        or message.get("orderNo")
        or message.get("order_no")
        or ""
    ).strip()
    if not order_no:
        return {"status": "failed", "code": "order_id_missing", "message": "付款提醒未提取到订单号"}

    async with async_session_maker() as db:
        account = (await db.execute(select(Account).where(Account.id == int(account_id)))).scalar_one_or_none()
        if account is None:
            return {"status": "failed", "code": "account_not_found", "message": "账号不存在"}
        account_settings = await load_account_settings(db, int(account.user_id), int(account.id))
        if account_settings.get("delivery_disabled"):
            return {"status": "blocked", "code": "delivery_disabled", "message": str(account_settings.get("delivery_disabled_reason") or "该账号已暂停发货")}

        item_id = str(message.get("itemId") or message.get("item_id") or "").strip() or None
        candidate = Order(
            account_id=account.id,
            order_no=order_no[:64],
            buyer_id=str(message.get("senderId") or "").strip() or None,
            item_external_id=item_id,
            quantity=1,
            amount=0.0,
            status="pending_ship",
        )
        try:
            card = await _find_card(db, candidate, account)
        except CardDeliveryError as exc:
            return {"status": "failed", "code": exc.code, "message": exc.message, "order_no": order_no, "item_id": item_id}
        payload = dict(card.payload or {})
        card_type = str(payload.get("type") or "text")
        stock = None
        if card_type == "data":
            stock = len([line for line in str(payload.get("data_content") or "").splitlines() if line.strip()])
        elif card_type == "text":
            stock = 1 if str(payload.get("text_content") or "").strip() else 0
        elif card_type == "image":
            stock = len(_card_image_paths(payload))
        return {
            "status": "would_send",
            "order_no": order_no,
            "item_id": item_id,
            "buyer_id": str(message.get("senderId") or "") or None,
            "card_id": card.id,
            "card_type": card_type,
            "stock_available": stock,
            "sent": False,
        }


async def auto_deliver_orders(
    db: AsyncSession,
    account: Account,
    *,
    source: str = "auto",
    placed_today_only: bool = False,
) -> dict[str, Any]:
    """处理当前账号所有未成功发货的订单。

    定时补发货传入 ``placed_today_only``，与旧版按真实下单时间过滤的
    规则一致；订单同步后的即时补发仍保留全量扫描行为。
    """
    statement = select(Order).where(
        Order.account_id == account.id,
        Order.status.in_(ELIGIBLE_ORDER_STATUSES),
        Order.card_only_delivered.is_(False),
    )
    if placed_today_only:
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        statement = statement.where(Order.placed_at.is_not(None), Order.placed_at >= today_start)
    rows = (await db.execute(statement.order_by(Order.placed_at.asc(), Order.id.asc()))).scalars().all()
    results = []
    for order in rows:
        if order.delivery_send_status == "success" or order.card_only_delivered:
            continue
        results.append(await deliver_order(db, order, account, source=source))
    return {
        "attempted": len(results),
        "sent": sum(1 for item in results if item.get("status") == "sent"),
        "failed": sum(1 for item in results if item.get("status") == "failed"),
        "results": results,
    }
