"""Goofish 采集任务：真实调用搜索接口并保存结果。"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models import Account, GoofishCrawlJob, GoofishCrawlResult
from common.services.goofish_client import GoofishClient

router = APIRouter(prefix="/api/v1/goofish/crawler", tags=["Goofish采集"])


class CrawlJobRequest(BaseModel):
    account_id: int
    cookie_id: int | None = None
    keyword: str = Field(min_length=1, max_length=255)
    interval_seconds: int = Field(default=900, ge=60, le=86400)
    start_page: int = Field(default=1, ge=1, le=100)
    pages: int = Field(default=1, ge=1, le=10)
    page_size: int = Field(default=20, ge=1, le=50)
    enabled: bool = True


def _uid(user) -> int:
    return int(user.get("sub", 1))


def _serialize_job(job: GoofishCrawlJob) -> dict:
    return {key: getattr(job, key) for key in ("id", "account_id", "keyword", "interval_seconds", "start_page", "pages", "page_size", "enabled", "last_run_at", "last_error", "created_at")}


async def _owned_job(job_id: int, uid: int, db: AsyncSession) -> GoofishCrawlJob:
    job = (await db.execute(select(GoofishCrawlJob).where(GoofishCrawlJob.id == job_id, GoofishCrawlJob.owner_id == uid))).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="采集任务不存在")
    return job


@router.get("/jobs")
async def list_jobs(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    jobs = (await db.execute(select(GoofishCrawlJob).where(GoofishCrawlJob.owner_id == _uid(user)).order_by(GoofishCrawlJob.id.desc()))).scalars().all()
    return ok({"jobs": [_serialize_job(job) for job in jobs], "total": len(jobs)}, "查询成功")


@router.post("/jobs")
async def create_job(payload: CrawlJobRequest, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    uid = _uid(user)
    account_id = payload.account_id if payload.account_id is not None else payload.cookie_id
    account = (await db.execute(select(Account).where(Account.id == account_id, Account.user_id == uid))).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    job = GoofishCrawlJob(owner_id=uid, account_id=account.id, keyword=payload.keyword.strip(), interval_seconds=payload.interval_seconds, start_page=payload.start_page, pages=payload.pages, page_size=payload.page_size, enabled=int(payload.enabled))
    db.add(job); await db.commit(); await db.refresh(job)
    return ok(_serialize_job(job), "采集任务已创建")


@router.post("/jobs/{job_id}/run-once")
async def run_once(job_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    job = await _owned_job(job_id, _uid(user), db)
    account = (await db.execute(select(Account).where(Account.id == job.account_id, Account.user_id == _uid(user)))).scalar_one_or_none()
    if account is None or not account.cookie:
        job.last_error = "账号 Cookie 不可用，请先扫码登录"
        await db.commit()
        raise HTTPException(status_code=409, detail=job.last_error)
    client = GoofishClient(account.cookie, account.proxy)
    collected: list[dict] = []
    try:
        for page in range(job.start_page, job.start_page + job.pages):
            result = await client.search(job.keyword, page, job.page_size)
            collected.extend(result["items"])
    except (ValueError, RuntimeError) as exc:
        job.last_error = str(exc); job.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None); await db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    for row in collected:
        if not row.get("item_id"):
            continue
        existing = (await db.execute(select(GoofishCrawlResult).where(GoofishCrawlResult.job_id == job.id, GoofishCrawlResult.external_id == row["item_id"]))).scalar_one_or_none()
        if existing is None:
            existing = GoofishCrawlResult(job_id=job.id, external_id=row["item_id"], title=row.get("title") or "未命名商品")
            db.add(existing)
        existing.title = row.get("title") or "未命名商品"; existing.price = row.get("price"); existing.seller = row.get("seller"); existing.area = row.get("area"); existing.url = row.get("url"); existing.payload = row.get("raw"); existing.fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
    job.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None); job.last_error = None
    await db.commit()
    return ok({"job_id": job.id, "collected": len(collected), "saved": len([row for row in collected if row.get("item_id")])}, "采集完成")


@router.get("/jobs/{job_id}/items")
async def list_items(job_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    await _owned_job(job_id, _uid(user), db)
    items = (await db.execute(select(GoofishCrawlResult).where(GoofishCrawlResult.job_id == job_id).order_by(GoofishCrawlResult.fetched_at.desc()).limit(200))).scalars().all()
    return ok({"items": [{key: getattr(item, key) for key in ("id", "external_id", "title", "price", "seller", "area", "url", "fetched_at")} for item in items]}, "查询成功")


@router.post("/jobs/{job_id}/{action}")
async def change_job(job_id: int, action: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    job = await _owned_job(job_id, _uid(user), db)
    if action == "start": job.enabled = 1
    elif action == "stop": job.enabled = 0
    else: raise HTTPException(status_code=422, detail="不支持的采集任务操作")
    await db.commit()
    return ok(_serialize_job(job), f"采集任务已{'启动' if action == 'start' else '停止'}")


@router.get("/jobs/{job_id}/status")
async def job_status(job_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    job = await _owned_job(job_id, _uid(user), db)
    return ok({"id": job.id, "enabled": bool(job.enabled), "running": False, "last_run_at": job.last_run_at, "last_error": job.last_error}, "查询成功")


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    job = await _owned_job(job_id, _uid(user), db)
    await db.execute(delete(GoofishCrawlResult).where(GoofishCrawlResult.job_id == job.id)); await db.delete(job); await db.commit()
    return ok({"id": job_id, "deleted": True}, "采集任务已删除")
