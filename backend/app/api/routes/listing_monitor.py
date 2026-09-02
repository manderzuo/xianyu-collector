# -*- coding: utf-8 -*-
"""商品上新/降价监控：任务、真实搜索采集、快照和执行日志。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import error, ok
from common.db.session import get_session
from common.models import Account, FeatureRecord
from common.services.goofish_client import GoofishClient

router = APIRouter(prefix="/api/v1/product-monitor/listing-tasks", tags=["商品监控"])
TASK_FEATURE = "listing-monitor-tasks"
LOG_FEATURE = "listing-monitor-logs"
ITEM_FEATURE = "listing-monitor-items"


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _payload(row: FeatureRecord) -> dict[str, Any]:
    return {**(row.payload or {}), "id": row.id, "owner_id": row.owner_id, "created_at": row.created_at, "updated_at": row.updated_at}


def _task(row: FeatureRecord) -> dict[str, Any]:
    data = _payload(row)
    data.setdefault("monitor_type", "listing")
    data.setdefault("keyword", "")
    data.setdefault("category_id", None)
    data.setdefault("price_min", None)
    data.setdefault("price_max", None)
    data.setdefault("publish_days", None)
    data.setdefault("interval_minutes", 10)
    data.setdefault("collect_pages", 1)
    data.setdefault("account_ids", [])
    data.setdefault("order_account_ids", [])
    data.setdefault("dm_content", "")
    data.setdefault("dm_batch_size", 10)
    data.setdefault("order_batch_size", 10)
    data.setdefault("direct_order", False)
    data.setdefault("is_enabled", True)
    data.setdefault("last_run_at", None)
    data.setdefault("dm_sent_count", 0)
    data.setdefault("ordered_count", 0)
    data.setdefault("duplicate_count", 0)
    return data


async def _owned(task_id: int, uid: int, db: AsyncSession) -> FeatureRecord:
    row = (await db.execute(select(FeatureRecord).where(FeatureRecord.id == task_id, FeatureRecord.feature == TASK_FEATURE, FeatureRecord.owner_id == uid))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="监控任务不存在")
    return row


@router.get("")
@router.get("/")
async def list_tasks(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), keyword: str | None = Query(None), is_enabled: bool | None = Query(None), category_id: int | None = Query(None),
    user=Depends(get_current_user), db: AsyncSession = Depends(get_session),
):
    uid = _uid(user)
    rows = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == TASK_FEATURE, FeatureRecord.owner_id == uid).order_by(FeatureRecord.id.desc()))).scalars().all())
    values = [_task(row) for row in rows]
    if keyword:
        values = [row for row in values if keyword.lower() in str(row.get("keyword") or "").lower()]
    if is_enabled is not None:
        values = [row for row in values if bool(row.get("is_enabled", True)) == is_enabled]
    if category_id is not None:
        values = [row for row in values if row.get("category_id") == category_id]
    total = len(values); start = (page - 1) * page_size
    return ok({"list": values[start:start + page_size], "items": values[start:start + page_size], "total": total, "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size if total else 0}, "查询成功")


@router.post("")
@router.post("/")
async def create_task(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    keyword = str(payload.get("keyword") or "").strip()
    if not keyword:
        return error("监控关键词不能为空", code="keyword_required")
    data = {**payload, "keyword": keyword, "monitor_type": str(payload.get("monitor_type") or "listing"), "is_enabled": bool(payload.get("is_enabled", True)), "last_run_at": None, "dm_sent_count": 0, "ordered_count": 0, "duplicate_count": 0}
    row = FeatureRecord(owner_id=_uid(user), feature=TASK_FEATURE, external_id=uuid4().hex, status="active", payload=data, note="监控任务已创建")
    db.add(row); await db.commit(); await db.refresh(row)
    return ok({"task": _task(row)}, "监控任务已创建")


@router.put("/{task_id}")
async def update_task(task_id: int, payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _owned(task_id, _uid(user), db)
    row.payload = {**(row.payload or {}), **payload}
    await db.commit(); await db.refresh(row)
    return ok({"task": _task(row)}, "监控任务已更新")


@router.put("/{task_id}/status")
async def update_task_status(task_id: int, payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _owned(task_id, _uid(user), db)
    values = {**(row.payload or {}), "is_enabled": bool(payload.get("is_enabled", payload.get("enabled", True)))}
    row.payload = values; row.status = "active" if values["is_enabled"] else "disabled"
    await db.commit(); await db.refresh(row)
    return ok({"task": _task(row)}, "监控任务状态已更新")


@router.post("/batch-delete")
async def batch_delete(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    ids = [int(value) for value in payload.get("ids") or [] if str(value).isdigit()]
    rows = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == TASK_FEATURE, FeatureRecord.owner_id == _uid(user), FeatureRecord.id.in_(ids)))).scalars().all()) if ids else []
    for row in rows:
        await db.delete(row)
    await db.commit()
    return ok({"success_count": len(rows), "total_count": len(ids)}, "监控任务已批量删除")


async def _update_task_fields(payload: dict[str, Any], field: str, value: Any, user: dict[str, Any], db: AsyncSession) -> int:
    ids = [int(item) for item in payload.get("ids") or [] if str(item).isdigit()]
    rows = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == TASK_FEATURE, FeatureRecord.owner_id == _uid(user), FeatureRecord.id.in_(ids)))).scalars().all()) if ids else []
    for row in rows:
        row.payload = {**(row.payload or {}), field: value}
    await db.commit()
    return len(rows)


@router.post("/batch-update-accounts")
async def batch_update_accounts(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    field = str(payload.get("field") or "account_ids")
    if field not in {"account_ids", "order_account_ids"}:
        raise HTTPException(422, "账号字段无效")
    count = await _update_task_fields(payload, field, [str(item) for item in payload.get("account_ids") or []], user, db)
    return ok({"success_count": count, "total_count": len(payload.get("ids") or [])}, "监控任务账号已更新")


@router.post("/batch-update-category")
async def batch_update_category(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    count = await _update_task_fields(payload, "category_id", payload.get("category_id"), user, db)
    return ok({"success_count": count, "total_count": len(payload.get("ids") or [])}, "监控任务分类已更新")


@router.post("/batch-update-dm-content")
async def batch_update_dm(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    count = await _update_task_fields(payload, "dm_content", str(payload.get("dm_content") or ""), user, db)
    return ok({"success_count": count, "total_count": len(payload.get("ids") or [])}, "监控私信内容已更新")


async def _run(row: FeatureRecord, user_id: int, db: AsyncSession) -> dict[str, Any]:
    task = _task(row)
    account_ids = [int(item) for item in task.get("account_ids") or [] if str(item).isdigit()]
    statement = select(Account).where(Account.user_id == user_id, Account.cookie.isnot(None), Account.cookie != "")
    if account_ids:
        statement = statement.where(Account.id.in_(account_ids))
    accounts = list((await db.execute(statement.order_by(Account.id.asc()))).scalars().all())
    if not accounts:
        raise HTTPException(409, "监控任务没有可用的已登录采集账号")
    fetched = saved = duplicates = 0
    used_accounts: list[str] = []
    try:
        for account in accounts:
            used_accounts.append(str(account.id))
            client = GoofishClient(account.cookie or "", account.proxy)
            for page in range(1, max(1, min(int(task.get("collect_pages") or 1), 20)) + 1):
                result = await client.search(str(task.get("keyword") or ""), page, 20)
                rows = result.get("items") or []
                fetched += len(rows)
                for value in rows:
                    item_id = str(value.get("item_id") or "").strip()
                    if not item_id:
                        continue
                    existing = None
                    items = (await db.execute(select(FeatureRecord).where(FeatureRecord.feature == ITEM_FEATURE, FeatureRecord.owner_id == user_id))).scalars().all()
                    for candidate in items:
                        candidate_payload = candidate.payload or {}
                        if str(candidate_payload.get("monitor_task_id")) == str(row.id) and str(candidate_payload.get("item_id")) == item_id:
                            existing = candidate; break
                    item_payload = {"monitor_task_id": row.id, "monitor_task_keyword": task.get("keyword"), "item_id": item_id, "title": value.get("title"), "price": value.get("price"), "area": value.get("area"), "pic_url": value.get("pic_url"), "seller_id": value.get("seller"), "target_url": value.get("url"), "is_dm_sent": False, "is_ordered": False, "last_seen_at": datetime.now(timezone.utc).isoformat(), "raw_json": value.get("raw") if isinstance(value.get("raw"), dict) else value}
                    if existing is None:
                        db.add(FeatureRecord(owner_id=user_id, feature=ITEM_FEATURE, external_id=item_id, status="active", payload=item_payload, note="监控采集到商品")); saved += 1
                    else:
                        existing.payload = {**(existing.payload or {}), **item_payload}; duplicates += 1
                await db.commit()
        row.payload = {**(row.payload or {}), "last_run_at": datetime.now(timezone.utc).isoformat(), "duplicate_count": int(task.get("duplicate_count") or 0) + duplicates}
        log_payload = {"monitor_task_id": row.id, "monitor_type": task.get("monitor_type"), "keyword": task.get("keyword"), "trigger_type": "manual", "used_account_ids": used_accounts, "pages": int(task.get("collect_pages") or 1), "fetched_count": fetched, "inserted_count": saved, "updated_count": duplicates, "status": "success", "message": "采集完成"}
        db.add(FeatureRecord(owner_id=user_id, feature=LOG_FEATURE, external_id=uuid4().hex, status="success", payload=log_payload, note="监控采集完成")); await db.commit()
    except Exception as exc:
        log_payload = {"monitor_task_id": row.id, "monitor_type": task.get("monitor_type"), "keyword": task.get("keyword"), "trigger_type": "manual", "used_account_ids": used_accounts, "pages": int(task.get("collect_pages") or 1), "fetched_count": fetched, "inserted_count": saved, "updated_count": duplicates, "status": "failed", "message": str(exc)[:1000]}
        db.add(FeatureRecord(owner_id=user_id, feature=LOG_FEATURE, external_id=uuid4().hex, status="failed", payload=log_payload, note=str(exc)[:1000])); await db.commit()
        raise
    return {"fetched_count": fetched, "inserted_count": saved, "updated_count": duplicates, "status": "success"}


@router.post("/{task_id}/run")
async def run_task(task_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _owned(task_id, _uid(user), db)
    result = await _run(row, _uid(user), db)
    return ok(result, "监控采集完成")


@router.get("/options")
async def task_options(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == TASK_FEATURE, FeatureRecord.owner_id == _uid(user)).order_by(FeatureRecord.id.desc()))).scalars().all())
    return ok({"list": [{"id": row.id, "keyword": str((row.payload or {}).get("keyword") or ""), "monitor_type": str((row.payload or {}).get("monitor_type") or "listing")} for row in rows]}, "查询成功")


@router.get("/overview")
async def overview(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    uid = _uid(user)
    tasks = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == TASK_FEATURE, FeatureRecord.owner_id == uid))).scalars().all())
    logs = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == LOG_FEATURE, FeatureRecord.owner_id == uid))).scalars().all())
    items = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == ITEM_FEATURE, FeatureRecord.owner_id == uid))).scalars().all())
    today = datetime.now(timezone.utc).date()
    today_logs = [row for row in logs if row.created_at and row.created_at.date() == today]
    return ok({"total_tasks": len(tasks), "enabled_tasks": sum(bool((row.payload or {}).get("is_enabled", True)) for row in tasks), "disabled_tasks": sum(not bool((row.payload or {}).get("is_enabled", True)) for row in tasks), "today_run_total": len(today_logs), "today_run_success": sum(row.status == "success" for row in today_logs), "today_run_partial": 0, "today_run_failed": sum(row.status == "failed" for row in today_logs), "today_collected": sum(int((row.payload or {}).get("inserted_count") or 0) for row in today_logs), "today_new": sum(int((row.payload or {}).get("inserted_count") or 0) for row in today_logs), "today_dm": 0, "today_dm_failed": 0, "today_ordered": 0, "today_order_failed": 0, "today_order_duplicate": 0, "total_items": len(items), "total_dm": 0, "total_ordered": 0}, "查询成功")


@router.get("/logs")
async def list_logs(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), monitor_task_id: int | None = Query(None), status: str | None = Query(None), monitor_type: str | None = Query(None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == LOG_FEATURE, FeatureRecord.owner_id == _uid(user)).order_by(FeatureRecord.id.desc()))).scalars().all())
    values = [_payload(row) for row in rows]
    if monitor_task_id is not None: values = [row for row in values if row.get("monitor_task_id") == monitor_task_id]
    if status: values = [row for row in values if row.get("status") == status]
    if monitor_type: values = [row for row in values if row.get("monitor_type") == monitor_type]
    total = len(values); start = (page - 1) * page_size
    return ok({"list": values[start:start + page_size], "total": total, "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size if total else 0}, "查询成功")


@router.delete("/logs/clear")
async def clear_logs(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)
    result = await db.execute(delete(FeatureRecord).where(FeatureRecord.feature == LOG_FEATURE, FeatureRecord.owner_id == _uid(user), FeatureRecord.created_at < cutoff)); await db.commit()
    return ok({"deleted_count": int(result.rowcount or 0)}, "10天前的监控日志已清理")


@router.get("/items")
async def list_items(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), monitor_task_id: int | None = Query(None), keyword: str | None = Query(None), item_id: str | None = Query(None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == ITEM_FEATURE, FeatureRecord.owner_id == _uid(user)).order_by(FeatureRecord.id.desc()))).scalars().all())
    values = [_payload(row) for row in rows]
    if monitor_task_id is not None: values = [row for row in values if row.get("monitor_task_id") == monitor_task_id]
    if keyword: values = [row for row in values if keyword.lower() in str(row.get("title") or "").lower() or keyword.lower() in str(row.get("monitor_task_keyword") or "").lower()]
    if item_id: values = [row for row in values if str(row.get("item_id")) == str(item_id)]
    total = len(values); start = (page - 1) * page_size
    return ok({"list": values[start:start + page_size], "total": total, "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size if total else 0}, "查询成功")


@router.get("/items/{item_id}")
async def get_item(item_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(FeatureRecord).where(FeatureRecord.id == item_id, FeatureRecord.feature == ITEM_FEATURE, FeatureRecord.owner_id == _uid(user)))).scalar_one_or_none()
    if row is None: raise HTTPException(404, "监控商品不存在")
    return ok({"item": _payload(row)}, "查询成功")


@router.post("/items/reset-dm")
async def reset_dm(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    ids = [int(value) for value in payload.get("ids") or [] if str(value).isdigit()]
    rows = list((await db.execute(select(FeatureRecord).where(FeatureRecord.id.in_(ids), FeatureRecord.feature == ITEM_FEATURE, FeatureRecord.owner_id == _uid(user)))).scalars().all()) if ids else []
    for row in rows: row.payload = {**(row.payload or {}), "is_dm_sent": False, "dm_status": "pending", "dm_fail_reason": None}
    await db.commit()
    return ok({"success_count": len(rows), "total_count": len(ids)}, "已重置私信状态")


__all__ = ["router"]
