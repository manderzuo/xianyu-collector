"""自动擦亮商品的实际执行逻辑。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select

from common.config import settings
from common.db.session import async_session_maker
from common.models import Account, AccountContent, FeatureRecord, PolishLog

MTOP_APP_KEY = "34839810"
MTOP_API = "mtop.taobao.idle.item.polish"
LOG_RETENTION_DAYS = 10
logger = logging.getLogger("xr.scheduler.polish")


def _cookies(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in str(value or "").split(";"):
        key, separator, item = part.strip().partition("=")
        if separator and key:
            result[key] = item
    return result


def _cookie_string(cookies: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def _sign(timestamp: str, token: str, data: str) -> str:
    return hashlib.md5(f"{token}&{timestamp}&{MTOP_APP_KEY}&{data}".encode()).hexdigest()


def _merge_response_cookies(headers: httpx.Headers, original: str) -> str:
    merged = _cookies(original)
    for value in headers.get_list("set-cookie"):
        first = value.split(";", 1)[0]
        key, separator, item = first.partition("=")
        if separator and key.strip():
            merged[key.strip()] = item.strip()
    return _cookie_string(merged)


async def _polish_item(client: httpx.AsyncClient, cookie: str, item_id: str, retry: int = 0) -> dict[str, Any]:
    parsed = _cookies(cookie)
    token_cookie = parsed.get("_m_h5_tk") or parsed.get("m_h5_tk") or ""
    if not token_cookie:
        return {"success": False, "message": "Cookie中没有找到_m_h5_tk", "cookie": cookie}
    token = token_cookie.split("_", 1)[0]
    data = json.dumps({"itemId": str(item_id)}, separators=(",", ":"), ensure_ascii=False)
    timestamp = str(int(time.time() * 1000))
    params = {
        "jsv": "2.7.2", "appKey": MTOP_APP_KEY, "t": timestamp,
        "sign": _sign(timestamp, token, data), "v": "2.0", "type": "originaljson",
        "accountSite": "xianyu", "dataType": "json", "timeout": "20000",
        "api": MTOP_API, "sessionOption": "AutoLoginOnly", "spm_cnt": "a21ybx.item.0.0",
        "spm_pre": "a21ybx.personal.feeds.1.42f86ac21eZ9zd", "log_id": "42f86ac21eZ9zd",
    }
    try:
        response = await client.post(
            f"{settings.goofish_mtop_host.rstrip('/')}/h5/{MTOP_API}/1.0/",
            params=params,
            data={"data": data},
            headers={
                "accept": "application/json",
                "accept-language": "en,zh-CN;q=0.9,zh;q=0.8,ru;q=0.7",
                "cache-control": "no-cache",
                "content-type": "application/x-www-form-urlencoded",
                "origin": "https://www.goofish.com",
                "pragma": "no-cache",
                "priority": "u=1, i",
                "sec-ch-ua": '"Google Chrome";v="141", "Not=A?Brand";v="8", "Not A(Brand)";v="141"',
                "sec-ch-ua-arch": '"x64"',
                "sec-ch-ua-bitness": '"64"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Win32"',
                "sec-ch-ua-platform-version": '"10.0.0"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
                "referer": "https://www.goofish.com/",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                "cookie": cookie.replace("\n", "").replace("\r", ""),
            },
        )
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"success": False, "message": f"网络请求失败：{str(exc)[:300]}", "cookie": cookie}
    updated_cookie = _merge_response_cookies(response.headers, cookie)
    ret = result.get("ret") if isinstance(result, dict) else []
    message = str(ret[0] if isinstance(ret, list) and ret else "未知错误")
    logger.info("擦亮接口返回 item_id=%s ret=%s", item_id, ret)
    if message == "SUCCESS::调用成功":
        return {
            "success": True,
            "message": "擦亮成功",
            "cookie": updated_cookie,
        }
    if "宝贝已经擦亮过了" in message or "IDLEITEM_POLISH_AGAIN" in message:
        # 这是平台明确拒绝重复操作，不能伪装成“本次擦亮成功”。
        return {
            "success": False,
            "already_polished": True,
            "message": "闲鱼返回商品今天已擦亮，未执行本次操作",
            "cookie": updated_cookie,
        }
    if any(word in message for word in ("TOKEN_EXPIRED", "TOKEN_EXOIRED")) and retry < 2 and updated_cookie != cookie:
        await asyncio.sleep(0.5)
        return await _polish_item(client, updated_cookie, item_id, retry + 1)
    return {"success": False, "message": message[:500], "cookie": updated_cookie}


async def execute_polish(account_id: int | None = None, *, force: bool = False) -> dict[str, Any]:
    batch_id = str(uuid.uuid4())
    started = datetime.now()
    success_count = failed_count = total_items = already_polished_count = 0
    async with async_session_maker() as db:
        await db.execute(delete(PolishLog).where(PolishLog.created_at < started - timedelta(days=LOG_RETENTION_DAYS)))
        account_query = select(Account).where(Account.status == "active", Account.cookie.isnot(None), Account.cookie != "")
        if account_id is not None:
            account_query = account_query.where(Account.id == account_id)
        accounts = list((await db.execute(account_query)).scalars().all())
        client_options: dict[str, Any] = {
            "timeout": httpx.Timeout(connect=15, read=30, write=15, pool=30),
            "follow_redirects": True,
        }
        if settings.goofish_proxy:
            client_options["proxy"] = settings.goofish_proxy
        async with httpx.AsyncClient(**client_options) as client:
            for account in accounts:
                setting = (await db.execute(select(FeatureRecord).where(FeatureRecord.owner_id == account.user_id, FeatureRecord.feature == "account-settings", FeatureRecord.external_id == str(account.id)))).scalar_one_or_none()
                if not setting or not bool((setting.payload or {}).get("auto_polish")):
                    continue
                items = list((await db.execute(select(AccountContent).where(AccountContent.account_id == account.id, AccountContent.content_type == "product"))).scalars().all())
                current_cookie = str(account.cookie or "")
                for item in items:
                    payload = dict(item.payload or {})
                    if not force and (
                        payload.get("last_polished_date") == started.date().isoformat()
                        or payload.get("last_polish_attempt_date") == started.date().isoformat()
                    ):
                        continue
                    total_items += 1
                    result = await _polish_item(client, current_cookie, item.external_id)
                    current_cookie = result.get("cookie") or current_cookie
                    is_success = bool(result.get("success"))
                    is_already_polished = bool(result.get("already_polished"))
                    if is_success:
                        success_count += 1
                        payload["last_polished_date"] = started.date().isoformat()
                        payload["last_polished_at"] = datetime.now().isoformat()
                        payload["is_polished"] = True
                        item.payload = payload
                    elif is_already_polished:
                        already_polished_count += 1
                        # 防止定时任务在同一天反复提交同一条被平台拒绝的请求，
                        # 但不更新 is_polished / last_polished_date，避免把未执行伪装成已完成。
                        payload["last_polish_attempt_date"] = started.date().isoformat()
                        item.payload = payload
                    else:
                        failed_count += 1
                    db.add(PolishLog(
                        batch_id=batch_id,
                        account_id=str(account.id),
                        item_id=str(item.external_id),
                        status="success" if is_success else ("skipped" if is_already_polished else "failed"),
                        error_message=None if is_success else str(result.get("message") or "未知错误"),
                    ))
                    if current_cookie != account.cookie:
                        account.cookie = current_cookie
                    await asyncio.sleep(2)
        await db.commit()
    return {
        "task_name": "refresh_listings", "status": "completed",
        "detail": "商品立即擦亮任务已执行" if account_id is not None else "自动擦亮任务已执行",
        "account_id": account_id,
        "batch_id": batch_id, "executed_at": started.isoformat(), "total_items": total_items,
        "success_count": success_count, "failed_count": failed_count,
        "fresh_success_count": success_count,
        "already_polished_count": already_polished_count,
    }
