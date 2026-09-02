"""人脸验证流程的可查询管理接口。"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models import FaceVerification

router = APIRouter(prefix="/api/v1/face-verification", tags=["人脸核验"])


def uid(user: dict) -> int: return int(user.get("sub", 1))


def serialize(item: FaceVerification) -> dict:
    return {"id": item.id, "account_id": item.account_id, "status": item.status, "provider_ref": item.provider_ref, "result_note": item.result_note, "created_at": item.created_at, "updated_at": item.updated_at}


async def owned(item_id: int, user: dict, db: AsyncSession) -> FaceVerification:
    item = (await db.execute(select(FaceVerification).where(FaceVerification.id == item_id, FaceVerification.owner_id == uid(user)))).scalar_one_or_none()
    if item is None: raise HTTPException(404, "人脸核验记录不存在")
    return item


@router.get("/notifications")
async def notifications(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    items = (await db.execute(select(FaceVerification).where(FaceVerification.owner_id == uid(user)).order_by(FaceVerification.id.desc()))).scalars().all()
    return ok({"items": [serialize(item) for item in items], "total": len(items)}, "查询成功")


@router.get("/notifications/{account_id}")
async def account_notifications(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    items = (await db.execute(select(FaceVerification).where(FaceVerification.owner_id == uid(user), FaceVerification.account_id == account_id).order_by(FaceVerification.id.desc()))).scalars().all()
    return ok({"items": [serialize(item) for item in items], "total": len(items)}, "查询成功")


@router.post("/start")
async def start(payload: dict, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = FaceVerification(owner_id=uid(user), account_id=payload.get("account_id"), status="pending", provider_ref=payload.get("provider_ref"))
    db.add(item); await db.commit(); await db.refresh(item)
    return ok(serialize(item), "人脸核验已登记，等待外部验证器回调")


@router.post("/notifications/{notification_id}/read")
async def mark_read(notification_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = await owned(notification_id, user, db); item.status = "read"; await db.commit()
    return ok(serialize(item), "核验通知已读")


@router.get("/screenshot/{account_id}")
async def screenshot(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = (await db.execute(select(FaceVerification).where(FaceVerification.owner_id == uid(user), FaceVerification.account_id == account_id).order_by(FaceVerification.id.desc()))).scalars().first()
    if item is None: raise HTTPException(404, "没有该账号的人脸核验记录")
    raise HTTPException(409, "当前核验器未提供截图")


@router.delete("/screenshot/{account_id}")
async def delete_screenshot(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = (await db.execute(select(FaceVerification).where(FaceVerification.owner_id == uid(user), FaceVerification.account_id == account_id).order_by(FaceVerification.id.desc()))).scalars().first()
    if item is None: raise HTTPException(404, "没有该账号的人脸核验记录")
    item.result_note = (item.result_note or "") + "\n截图清理请求：" + datetime.now(timezone.utc).isoformat(); await db.commit()
    return ok({"account_id": account_id, "deleted": True}, "截图清理请求已记录")
