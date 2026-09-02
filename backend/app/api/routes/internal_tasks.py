# -*- coding: utf-8 -*-
"""供独立调度器调用的内部任务入口。

调度器与 Web 后端是两个容器，不能直接导入后端服务层。这里使用共享的
内部鉴权令牌把任务调用回 Web 后端，实际发货仍由后端复用订单、卡券和
闲鱼 IM 的同一套业务逻辑完成。
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.services.account_settings import load_account_settings
from backend.app.services.card_delivery import auto_deliver_orders
from common.config import settings
from common.db.session import async_session_maker
from common.models import Account, FeatureRecord

router = APIRouter(prefix="/internal/tasks", tags=["内部任务"])


def _check_internal_token(token: str | None) -> None:
    if not token or not secrets.compare_digest(token, settings.jwt_secret):
        raise HTTPException(status_code=401, detail="内部任务鉴权失败")


def _result_status(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "failed")
    if status == "sent":
        return "success"
    if status in {"skipped", "sending"}:
        return "skipped"
    return "failed"


def _log_payload(batch_id: str, account: Account, result: dict[str, Any]) -> dict[str, Any]:
    status = _result_status(result)
    return {
        "batch_id": batch_id,
        "account_id": account.id,
        "account_name": account.account_name,
        "order_no": str(result.get("order_no") or ""),
        "status": status,
        "error_message": None if status != "failed" else str(result.get("message") or result.get("detail") or "发货失败")[:2000],
        "delivery_status": str(result.get("status") or ""),
        "card_id": result.get("card_id"),
        "message_ids": result.get("message_ids") or [],
    }


@router.post("/redelivery")
async def run_redelivery(x_internal_token: str | None = Header(default=None, alias="X-Internal-Token")):
    """处理所有开启旧版定时补发货开关的账号，并落库批次与明细。"""
    _check_internal_token(x_internal_token)
    batch_id = str(uuid4())
    executed_at = datetime.utcnow()
    cutoff = executed_at - timedelta(days=10)
    account_summaries: list[dict[str, Any]] = []
    all_logs: list[tuple[Account, dict[str, Any]]] = []
    selected_accounts: list[Account] = []

    async with async_session_maker() as db:
        # 与旧版保持一致：日志只保留十天，清理动作本身不影响订单状态。
        await db.execute(
            delete(FeatureRecord).where(
                FeatureRecord.feature.in_(["redelivery-batches", "redelivery-logs"]),
                FeatureRecord.created_at < cutoff,
            )
        )
        accounts = list(
            (
                await db.execute(
                    select(Account)
                    .where(Account.status == "active", Account.cookie.isnot(None), Account.cookie != "")
                    .order_by(Account.id.asc())
                )
            )
            .scalars()
            .all()
        )
        for account in accounts:
            account_settings = await load_account_settings(db, int(account.user_id), int(account.id))
            # 旧版任务只有在“自动确认收货”或“仅发卡券”开启时才会执行。
            if not account_settings.get("scheduled_redelivery"):
                continue
            if not (account_settings.get("auto_confirm") or account_settings.get("only_send_card")):
                continue
            selected_accounts.append(account)
            try:
                result = await auto_deliver_orders(db, account, source="scheduled", placed_today_only=True)
                account_summaries.append({
                    "account_id": account.id,
                    "account_name": account.account_name,
                    "status": "completed",
                    "attempted": int(result.get("attempted") or 0),
                    "sent": int(result.get("sent") or 0),
                    "failed": int(result.get("failed") or 0),
                    "results": result.get("results") or [],
                })
                for delivery_result in result.get("results") or []:
                    if isinstance(delivery_result, dict):
                        all_logs.append((account, delivery_result))
            except Exception as exc:  # 单账号失败不能阻断其他账号
                message = f"账号处理异常：{str(exc)[:500]}"
                account_summaries.append({
                    "account_id": account.id,
                    "account_name": account.account_name,
                    "status": "failed",
                    "attempted": 0,
                    "sent": 0,
                    "failed": 0,
                    "error_message": message,
                    "results": [],
                })

        total_orders = 0
        success_count = 0
        failed_count = 0
        skipped_count = 0
        for account, delivery_result in all_logs:
            payload = _log_payload(batch_id, account, delivery_result)
            status = payload["status"]
            total_orders += 1
            if status == "success":
                success_count += 1
            elif status == "failed":
                failed_count += 1
            else:
                skipped_count += 1
            db.add(
                FeatureRecord(
                    owner_id=int(account.user_id),
                    feature="redelivery-logs",
                    external_id=uuid4().hex,
                    status=status,
                    payload=payload,
                    note=payload.get("error_message") or "定时补发货处理完成",
                )
            )

        failed_accounts = sum(1 for item in account_summaries if item.get("status") == "failed")
        batch_status = "failed" if failed_accounts and not total_orders else ("partial" if failed_count or failed_accounts else "completed")
        owner_id = int(selected_accounts[0].user_id) if selected_accounts else 1
        batch_payload = {
            "batch_id": batch_id,
            "executed_at": executed_at.isoformat(),
            "status": batch_status,
            "account_count": len(selected_accounts),
            "failed_account_count": failed_accounts,
            "total_orders": total_orders,
            "success_count": success_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "account_results": account_summaries,
        }
        db.add(
            FeatureRecord(
                owner_id=owner_id,
                feature="redelivery-batches",
                external_id=batch_id,
                status=batch_status,
                payload=batch_payload,
                note="定时补发货任务执行记录",
            )
        )
        await db.commit()

    return {
        "success": batch_status in {"completed", "partial"},
        "code": batch_status,
        "message": "定时补发货执行完成" if batch_status == "completed" else "定时补发货部分完成" if batch_status == "partial" else "定时补发货执行失败",
        "data": batch_payload,
    }
