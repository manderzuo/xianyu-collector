"""调度器触发后台的真实商品/订单同步链路。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select

from common.config import settings
from common.db.session import async_session_maker
from common.models import Account


async def execute_account_sync(
    mode: str,
    *,
    account_id: int | None = None,
) -> dict[str, Any]:
    """通过内部接口逐账号执行商品或订单同步。

    同步逻辑和卡券自动发货都由 backend 统一执行，避免 scheduler 与手动
    同步各维护一份平台协议，造成结果不一致。
    """
    mode = str(mode or "all").lower()
    if mode not in {"products", "orders", "all"}:
        return {"task_name": f"sync_{mode}", "status": "failed", "detail": "同步模式无效"}

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
    base_url = settings.backend_service_url.rstrip("/")
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=90, write=20, pool=30)) as client:
        for account in accounts:
            try:
                response = await client.post(
                    f"{base_url}/api/v1/internal/accounts/{account.id}/sync",
                    json={"mode": mode, "page_size": 30, "max_pages": 100},
                    headers={"X-Internal-Token": settings.jwt_secret},
                )
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                success = bool(response.is_success and payload.get("success"))
                results.append({
                    "account_id": account.id,
                    "success": success,
                    "status": "success" if success else "failed",
                    "message": str(payload.get("message") or payload.get("detail") or "同步失败")[:500],
                    "data": payload.get("data") if success else None,
                })
            except (httpx.HTTPError, ValueError) as exc:
                results.append({
                    "account_id": account.id,
                    "success": False,
                    "status": "failed",
                    "message": f"同步请求异常：{str(exc)[:500]}",
                })

    success_count = sum(1 for item in results if item.get("success"))
    failed_count = len(results) - success_count
    return {
        "task_name": f"sync_{mode}",
        "status": "partial" if failed_count else "completed",
        "detail": f"{mode}同步处理 {len(results)} 个账号，成功 {success_count} 个",
        "success_count": success_count,
        "failed_count": failed_count,
        "results": results,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }


__all__ = ["execute_account_sync"]
