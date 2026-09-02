# -*- coding: utf-8 -*-
"""公告和弹窗公告的真实管理接口。"""
from __future__ import annotations

from html import escape
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models import Announcement, FeatureRecord, Popup

announcement_router = APIRouter(prefix="/api/v1/announcements", tags=["公告管理"])
popup_router = APIRouter(prefix="/api/v1/popup-announcements", tags=["弹窗公告"])


def _uid(user: dict) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _is_admin(user: dict) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _require_admin(user: dict) -> None:
    if not _is_admin(user):
        raise HTTPException(403, "仅管理员可以管理公告")


def _announcement(row: Announcement) -> dict[str, Any]:
    return {
        "id": row.id, "title": row.title, "content": row.content, "level": row.level,
        "status": row.status, "source": "local",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@announcement_router.get("/public")
async def public_announcements(db: AsyncSession = Depends(get_session)):
    rows = list((await db.execute(
        select(Announcement).where(Announcement.status == 1).order_by(desc(Announcement.id)).limit(20)
    )).scalars().all())
    return ok({"items": [_announcement(row) for row in rows], "total": len(rows)}, "查询成功")


@announcement_router.get("")
async def list_announcements(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    user=Depends(get_current_user), db: AsyncSession = Depends(get_session),
):
    del user
    visible = Announcement.status == 1
    total = int((await db.execute(select(func.count()).select_from(Announcement).where(visible))).scalar_one() or 0)
    rows = list((await db.execute(
        select(Announcement).where(visible).order_by(desc(Announcement.id)).offset((page - 1) * page_size).limit(page_size)
    )).scalars().all())
    return ok({"items": [_announcement(row) for row in rows], "total": total, "page": page, "page_size": page_size}, "查询成功")


@announcement_router.post("")
async def create_announcement(payload: dict[str, Any] = Body(...), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _require_admin(user)
    title = str(payload.get("title") or "").strip(); content = str(payload.get("content") or "").strip()
    if not title or not content:
        raise HTTPException(422, "公告标题和内容不能为空")
    row = Announcement(title=escape(title), content=escape(content), level=str(payload.get("level") or "info")[:16], status=1)
    db.add(row); await db.commit(); await db.refresh(row)
    return ok(_announcement(row), "公告发布成功")


@announcement_router.put("/{announcement_id}")
async def update_announcement(announcement_id: int, payload: dict[str, Any] = Body(...), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _require_admin(user)
    row = await db.get(Announcement, announcement_id)
    if row is None:
        raise HTTPException(404, "公告不存在")
    title = str(payload.get("title") or "").strip(); content = str(payload.get("content") or "").strip()
    if not title or not content:
        raise HTTPException(422, "公告标题和内容不能为空")
    row.title = escape(title); row.content = escape(content)
    if "status" in payload: row.status = 1 if str(payload.get("status")).lower() in {"1", "active", "enabled", "true"} else 0
    await db.commit(); await db.refresh(row)
    return ok(_announcement(row), "公告更新成功")


@announcement_router.delete("/{announcement_id}")
async def delete_announcement(announcement_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _require_admin(user)
    row = await db.get(Announcement, announcement_id)
    if row is None:
        raise HTTPException(404, "公告不存在")
    row.status = 0
    await db.commit()
    return ok({"id": announcement_id, "deleted": True}, "公告已删除")


def _popup(row: FeatureRecord) -> dict[str, Any]:
    value = dict(row.payload or {})
    enabled = value.get("is_enabled")
    if enabled is None: enabled = row.status not in {"disabled", "inactive", "0"}
    return {
        "id": row.id, "title": str(value.get("title") or "工作台提示"), "content": str(value.get("content") or ""),
        "link": value.get("link"), "level": str(value.get("level") or "info"), "is_enabled": bool(enabled),
        "source": "local", "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _popup_query(db: AsyncSession):
    return select(FeatureRecord).where(FeatureRecord.feature == "popup-announcements").order_by(desc(FeatureRecord.id))


@popup_router.get("/public")
async def public_popups(db: AsyncSession = Depends(get_session)):
    rows = list((await db.execute(_popup_query(db).limit(20))).scalars().all())
    items = [item for item in (_popup(row) for row in rows) if item["is_enabled"]]
    if not items:
        legacy = list((await db.execute(select(Popup).where(Popup.status == 1).order_by(desc(Popup.id)).limit(20))).scalars().all())
        items = [{"id": row.id, "title": row.title, "content": row.content, "link": None, "level": row.level, "is_enabled": True, "source": "local", "created_at": row.created_at.isoformat() if row.created_at else None, "updated_at": row.updated_at.isoformat() if row.updated_at else None} for row in legacy]
    return ok({"items": items, "total": len(items)}, "查询成功")


@popup_router.get("")
async def list_popups(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _require_admin(user)
    rows = list((await db.execute(_popup_query(db))).scalars().all())
    total = len(rows); values = rows[(page - 1) * page_size: page * page_size]
    return ok({"items": [_popup(row) for row in values], "total": total, "page": page, "page_size": page_size}, "查询成功")


@popup_router.post("")
async def create_popup(payload: dict[str, Any] = Body(...), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _require_admin(user)
    title = str(payload.get("title") or "").strip(); content = str(payload.get("content") or "").strip()
    if not title or not content:
        raise HTTPException(422, "弹窗标题和内容不能为空")
    enabled = bool(payload.get("is_enabled", True))
    row = FeatureRecord(owner_id=_uid(user), feature="popup-announcements", external_id=uuid4().hex, status="active" if enabled else "inactive", payload={"title": escape(title), "content": escape(content), "link": str(payload.get("link") or "").strip() or None, "is_enabled": enabled}, note="弹窗公告")
    db.add(row); await db.commit(); await db.refresh(row)
    return ok(_popup(row), "弹窗公告发布成功")


@popup_router.put("/{popup_id}/toggle")
async def toggle_popup(popup_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _require_admin(user)
    row = (await db.execute(_popup_query(db).where(FeatureRecord.id == popup_id))).scalar_one_or_none()
    if row is None: raise HTTPException(404, "弹窗公告不存在")
    value = dict(row.payload or {}); enabled = not bool(value.get("is_enabled", row.status == "active")); value["is_enabled"] = enabled; row.payload = value; row.status = "active" if enabled else "inactive"
    await db.commit()
    return ok({"is_enabled": enabled}, "弹窗公告已" + ("启用" if enabled else "停用"))


@popup_router.put("/{popup_id}")
async def update_popup(popup_id: int, payload: dict[str, Any] = Body(...), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _require_admin(user)
    row = (await db.execute(_popup_query(db).where(FeatureRecord.id == popup_id))).scalar_one_or_none()
    if row is None: raise HTTPException(404, "弹窗公告不存在")
    title = str(payload.get("title") or "").strip(); content = str(payload.get("content") or "").strip()
    if not title or not content: raise HTTPException(422, "弹窗标题和内容不能为空")
    value = {**(row.payload or {}), "title": escape(title), "content": escape(content), "link": str(payload.get("link") or "").strip() or None}
    if "is_enabled" in payload: value["is_enabled"] = bool(payload.get("is_enabled"))
    row.payload = value; row.status = "active" if bool(value.get("is_enabled", True)) else "inactive"
    await db.commit(); await db.refresh(row)
    return ok(_popup(row), "弹窗公告已更新")


@popup_router.delete("/{popup_id}")
async def delete_popup(popup_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _require_admin(user)
    row = (await db.execute(_popup_query(db).where(FeatureRecord.id == popup_id))).scalar_one_or_none()
    if row is None: raise HTTPException(404, "弹窗公告不存在")
    await db.delete(row); await db.commit()
    return ok({"id": popup_id, "deleted": True}, "弹窗公告已删除")
