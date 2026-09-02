# -*- coding: utf-8 -*-
"""账号 Cookie 与 IM Token 的真实续期任务。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select

from common.config import settings
from common.db.session import async_session_maker
from common.models.accounts import Account
from common.services.account_renewal import renew_account_session


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def execute_cookie_renewal(
    account_id: int | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """续期启用或已过期账号；手动触发时 ``force=True``。"""
    async with async_session_maker() as session:
        statement = select(Account).where(
            Account.cookie.isnot(None),
            Account.cookie != "",
            Account.status.in_(["active", "expired"]),
        ).order_by(Account.id.asc())
        if account_id is not None:
            statement = statement.where(Account.id == account_id)
        accounts = list((await session.execute(statement)).scalars().all())
        results: list[dict[str, Any]] = []
        for account in accounts:
            try:
                results.append(
                    await renew_account_session(
                        session,
                        account,
                        source="manual" if force else "scheduled_task",
                        force=force or account.status == "expired",
                        notify_runtime=True,
                    )
                )
            except Exception as exc:
                await session.rollback()
                results.append({
                    "account_id": account.id,
                    "success": False,
                    "status": "failed",
                    "method": "none",
                    "message": f"续期执行异常：{str(exc)[:500]}",
                    "updated_cookie_names": [],
                })

    success_count = sum(1 for item in results if item.get("success") and item.get("status") != "skipped")
    skipped_count = sum(1 for item in results if item.get("status") == "skipped")
    failed_count = len(results) - success_count - skipped_count
    task_status = "partial" if failed_count else "completed"
    return {
        "task_name": "refresh_cookies",
        "status": task_status,
        "detail": f"Cookie续期处理 {len(results)} 个账号，成功 {success_count} 个，跳过 {skipped_count} 个，失败 {failed_count} 个",
        "success_count": success_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "results": results,
        "executed_at": _now(),
    }


async def execute_token_refresh(account_id: int | None = None) -> dict[str, Any]:
    """让连接服务重新获取 IM Token，避免把 IM Token 与登录 Cookie 混为一谈。"""
    async with async_session_maker() as session:
        statement = select(Account).where(
            Account.status == "active",
            Account.cookie.isnot(None),
            Account.cookie != "",
        ).order_by(Account.id.asc())
        if account_id is not None:
            statement = statement.where(Account.id == account_id)
        accounts = list((await session.execute(statement)).scalars().all())

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=30, write=10, pool=20)) as client:
        for account in accounts:
            try:
                response = await client.post(
                    f"{settings.websocket_service_url.rstrip('/')}/internal/accounts/{account.id}/restart",
                    json={"cookie_value": account.cookie or "", "user_id": int(account.user_id)},
                    headers={"X-Internal-Token": settings.jwt_secret},
                )
                payload = response.json()
                results.append({
                    "account_id": account.id,
                    "success": bool(response.is_success and payload.get("success")),
                    "status": "restarting" if response.is_success and payload.get("success") else "failed",
                    "message": str(payload.get("message") or "IM Token刷新请求失败")[:500],
                })
            except (httpx.HTTPError, ValueError) as exc:
                results.append({
                    "account_id": account.id,
                    "success": False,
                    "status": "failed",
                    "message": f"Token刷新请求异常：{str(exc)[:500]}",
                })

    success_count = sum(1 for item in results if item.get("success"))
    return {
        "task_name": "refresh_tokens",
        "status": "partial" if len(results) != success_count else "completed",
        "detail": f"IM Token刷新已提交 {len(results)} 个账号，成功 {success_count} 个",
        "success_count": success_count,
        "failed_count": len(results) - success_count,
        "results": results,
        "executed_at": _now(),
    }


__all__ = ["execute_cookie_renewal", "execute_token_refresh"]
