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
        # 必须包含 expired 账号：``sync_account_products`` 内部的
        # “会话失效 → 强制续期 → 重试同步”自愈逻辑就在这条路径上。
        # 早期只选 active，导致账号一旦被标记过期就再也拿不到任何网页侧
        # 恢复尝试，把一次局部故障放大成永久停摆。
        # 续期频率由 account_renewal 的冷却窗口统一限制，不会因此打高频。
        statement = select(Account).where(
            Account.status.in_(["active", "expired"]),
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
                    # 闲鱼商品列表接口的单页上限实测为 20；传 30 会直接返回
                    # FAIL_BIZ_FORBIDDEN，导致定时同步看似执行但实际没有入库。
                    json={"mode": mode, "page_size": 20, "max_pages": 100},
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
    if not results:
        # 没有可处理账号时必须报 skipped。此前 0 个账号会让 failed_count 为 0
        # 从而判定 completed，界面上看起来“任务成功”，实际什么都没做。
        return {
            "task_name": f"sync_{mode}",
            "status": "skipped",
            "detail": f"{mode}同步没有可处理的账号（无 active/expired 账号）",
            "success_count": 0,
            "failed_count": 0,
            "results": [],
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
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
