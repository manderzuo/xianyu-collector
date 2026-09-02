# -*- coding: utf-8 -*-
"""账号登录态续期的数据库与运行时收口。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.config import settings
from common.models.account_cookies import AccountCookie
from common.models.accounts import Account
from common.models.feature_records import FeatureRecord
from common.services.cookie_renewal import (
    CookieRenewalResult,
    cookie_renewal_service,
    is_session_expired_message,
)

logger = logging.getLogger("xr.account_renewal")


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _notify_runtime(account: Account, action: str = "restart") -> dict[str, Any]:
    """让 WebSocket 运行时使用新 Cookie；通知失败不回滚已经成功的续期。"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=20, write=10, pool=20)) as client:
            response = await client.post(
                f"{settings.websocket_service_url.rstrip('/')}/internal/accounts/{account.id}/{action}",
                json={"cookie_value": account.cookie or "", "user_id": int(account.user_id)},
                headers={"X-Internal-Token": settings.jwt_secret},
            )
        payload = response.json()
        if response.is_success and payload.get("success"):
            return payload.get("data") or {"status": "accepted"}
        return {"status": "failed", "error": str(payload.get("message") or "WebSocket拒绝重启")[:500]}
    except (httpx.HTTPError, ValueError) as exc:
        return {"status": "unavailable", "error": str(exc)[:500]}


async def renew_account_session(
    session: AsyncSession,
    account: Account,
    *,
    source: str = "scheduled_task",
    force: bool = False,
    notify_runtime: bool = True,
    observed_session_expired: bool = False,
) -> dict[str, Any]:
    """续期一个账号并把结果写回数据库。

    ``force=False`` 时，已在未来 24 小时内续期过的健康账号跳过，避免定时
    任务频繁请求 Passport；Session 过期和手动续期会强制执行。
    """
    old_cookie = str(account.cookie or "").strip()
    now = _now_naive()
    if not force and account.status == "active" and account.cookie_expire_at:
        if account.cookie_expire_at > now + timedelta(hours=24):
            return {
                "account_id": account.id,
                "success": True,
                "status": "skipped",
                "method": "none",
                "message": "登录态仍在有效期内，跳过本次续期",
                "updated_cookie_names": [],
                "runtime": None,
            }

    account_settings: dict[str, Any] = {}
    settings_row = (
        await session.execute(
            select(FeatureRecord).where(
                FeatureRecord.owner_id == int(account.user_id),
                FeatureRecord.feature == "account-settings",
                FeatureRecord.external_id == str(account.id),
            )
        )
    ).scalar_one_or_none()
    if settings_row is not None and isinstance(settings_row.payload, dict):
        account_settings = settings_row.payload

    result: CookieRenewalResult = await cookie_renewal_service.renew(
        old_cookie,
        str(account.id),
        allow_browser=True,
        username=str(account_settings.get("username") or ""),
        password=str(account_settings.get("login_password") or ""),
        show_browser=bool(account_settings.get("show_browser")),
    )
    new_cookie = str(result.new_cookie or old_cookie).strip()
    cookie_changed = bool(new_cookie and new_cookie != old_cookie)
    if cookie_changed:
        account.cookie = new_cookie
        session.add(AccountCookie(
            account_id=account.id,
            cookie_value=new_cookie,
            status="active" if result.success else "stale",
            expires_at=now + timedelta(days=30) if result.success else None,
        ))

    if result.success:
        # 手动暂停不被续期任务强制改成 active；expired 才是登录态生命周期状态。
        if account.status == "expired":
            account.status = "active"
        account.cookie_expire_at = now + timedelta(days=30)
    elif observed_session_expired:
        # 运行时已经从闲鱼接口确认 Session 失效时，不能继续相信本地缓存的
        # cookie_expire_at。将账号置为 expired，确保下一轮定时任务不会因本地
        # 到期时间尚未到而跳过强制续期。
        account.status = "expired"
        account.cookie_expire_at = now
    elif result.needs_manual_login and (
        not old_cookie
        or is_session_expired_message(result.message)
        or "Cookie为空" in result.message
        or "Cookie缺少" in result.message
    ):
        # 只有确认平台登录态已经失效时才把账号置为 expired；网络异常、
        # 浏览器 profile 暂不可用等可恢复错误保留 active，避免误杀在线账号。
        account.status = "expired"

    await session.commit()
    runtime: dict[str, Any] | None = None
    if result.success and notify_runtime:
        runtime = await _notify_runtime(account, "restart")

    return {
        "account_id": account.id,
        "success": result.success,
        "status": "success" if result.success else "needs_manual_login" if result.needs_manual_login else "failed",
        "method": result.method,
        "message": result.message,
        "updated_cookie_names": result.updated_cookie_names,
        "cookie_changed": cookie_changed,
        "needs_manual_login": result.needs_manual_login,
        "steps": result.steps,
        "runtime": runtime,
    }


__all__ = ["renew_account_session"]
