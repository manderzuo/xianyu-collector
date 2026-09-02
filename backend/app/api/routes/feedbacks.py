"""意见反馈的真实持久化接口。

反馈页面需要的不只是一个“提交成功”的动作，还包括列表、详情、连续回复、
解决状态和管理员删除。重写版使用结构化 FeatureRecord 保存这些字段，兼容
旧数据库的同时不再把反馈写入通用兜底路由。
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models import FeatureRecord

router = APIRouter(prefix="/api/v1/feedbacks", tags=["意见反馈"])
FEATURE = "feedbacks"


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _clean(value: Any, limit: int = 5000) -> str:
    return html.escape(str(value or "").strip())[:limit]


def _payload(row: FeatureRecord) -> dict[str, Any]:
    return dict(row.payload or {})


def _row(row: FeatureRecord) -> dict[str, Any]:
    value = _payload(row)
    replies = value.get("replies") if isinstance(value.get("replies"), list) else []
    return {
        "id": row.id,
        "user_id": row.owner_id,
        "cookie_id": value.get("cookie_id"),
        "title": str(value.get("title") or "未命名反馈"),
        "content": str(value.get("content") or ""),
        "feedback_type": str(value.get("feedback_type") or "OTHER"),
        "images": value.get("images") if isinstance(value.get("images"), list) else [],
        "is_resolved": bool(value.get("is_resolved", row.status == "resolved")),
        "resolved_at": value.get("resolved_at"),
        "admin_reply": next((item.get("content") for item in reversed(replies) if isinstance(item, dict) and item.get("is_admin")), None),
        "message_count": len(replies) + 1,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


async def _get(feedback_id: int, user: dict[str, Any], db: AsyncSession) -> FeatureRecord:
    statement = select(FeatureRecord).where(FeatureRecord.id == feedback_id, FeatureRecord.feature == FEATURE)
    if not _admin(user):
        statement = statement.where(FeatureRecord.owner_id == _uid(user))
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "反馈不存在或无权访问")
    return row


@router.get("/stats")
async def feedback_stats(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    statement = select(FeatureRecord).where(FeatureRecord.feature == FEATURE)
    if not _admin(user):
        statement = statement.where(FeatureRecord.owner_id == _uid(user))
    rows = list((await db.execute(statement)).scalars().all())
    resolved = sum(bool(_payload(row).get("is_resolved", row.status == "resolved")) for row in rows)
    return ok({"total": len(rows), "resolved": resolved, "pending": len(rows) - resolved})


@router.get("")
@router.get("/")
async def list_feedbacks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    is_resolved: bool | None = None,
    feedback_type: str | None = None,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    statement = select(FeatureRecord).where(FeatureRecord.feature == FEATURE).order_by(desc(FeatureRecord.id))
    if not _admin(user):
        statement = statement.where(FeatureRecord.owner_id == _uid(user))
    rows = list((await db.execute(statement)).scalars().all())
    values = [_row(row) for row in rows]
    if is_resolved is not None:
        values = [item for item in values if item["is_resolved"] is is_resolved]
    if feedback_type:
        values = [item for item in values if item["feedback_type"] == feedback_type]
    total = len(values)
    start = (page - 1) * page_size
    return ok({"items": values[start:start + page_size], "total": total, "page": page, "page_size": page_size})


@router.get("/{feedback_id}")
async def feedback_detail(feedback_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _get(feedback_id, user, db)
    value = _payload(row)
    messages = [{
        "id": 0,
        "is_admin": False,
        "content": str(value.get("content") or ""),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }]
    replies = value.get("replies") if isinstance(value.get("replies"), list) else []
    messages.extend(item for item in replies if isinstance(item, dict))
    return ok({**_row(row), "messages": messages})


@router.post("")
@router.post("/")
async def create_feedback(request: Request, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    value = await _body(request)
    params = request.query_params
    title = _clean(value.get("title") or params.get("title"), 255)
    content = _clean(value.get("content") or params.get("content"), 10000)
    feedback_type = str(value.get("feedback_type") or params.get("feedback_type") or "OTHER").upper()
    if feedback_type not in {"FEATURE", "BUG", "OTHER"}:
        raise HTTPException(422, "无效的反馈类型")
    if not title or not content:
        raise HTTPException(422, "反馈标题和内容不能为空")
    images = value.get("images") or params.get("images") or []
    if isinstance(images, str):
        try:
            images = json.loads(images)
        except ValueError:
            images = []
    if not isinstance(images, list):
        images = []
    row = FeatureRecord(
        owner_id=_uid(user), feature=FEATURE, external_id=uuid4().hex, status="open",
        payload={
            "title": title, "content": content, "feedback_type": feedback_type,
            "cookie_id": value.get("cookie_id") or params.get("cookie_id"),
            "images": [str(item)[:500] for item in images[:9]], "is_resolved": False,
            "resolved_at": None, "replies": [],
        }, note="用户意见反馈",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return ok({"id": row.id}, "反馈提交成功")


@router.post("/{feedback_id}/reply")
async def reply_feedback(feedback_id: int, request: Request, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = await _get(feedback_id, user, db)
    value = await _body(request)
    content = _clean(value.get("content") or request.query_params.get("content"), 5000)
    if not content:
        raise HTTPException(422, "回复内容不能为空")
    data = _payload(row)
    replies = data.get("replies") if isinstance(data.get("replies"), list) else []
    replies.append({"id": uuid4().hex, "is_admin": _admin(user), "content": content, "created_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()})
    data["replies"] = replies
    row.payload = data
    await db.commit()
    return ok({"id": row.id, "message_count": len(replies) + 1}, "回复成功")


@router.put("/{feedback_id}/resolve")
async def resolve_feedback(feedback_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    if not _admin(user):
        raise HTTPException(403, "仅管理员可以标记反馈状态")
    row = await _get(feedback_id, user, db)
    data = _payload(row); data.update({"is_resolved": True, "resolved_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()})
    row.payload = data; row.status = "resolved"
    await db.commit()
    return ok(_row(row), "已标记为解决")


@router.put("/{feedback_id}/unresolve")
async def unresolve_feedback(feedback_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    if not _admin(user):
        raise HTTPException(403, "仅管理员可以标记反馈状态")
    row = await _get(feedback_id, user, db)
    data = _payload(row); data.update({"is_resolved": False, "resolved_at": None})
    row.payload = data; row.status = "open"
    await db.commit()
    return ok(_row(row), "已标记为未解决")


@router.delete("/{feedback_id}")
async def delete_feedback(feedback_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    if not _admin(user):
        raise HTTPException(403, "仅管理员可以删除反馈")
    row = await _get(feedback_id, user, db)
    await db.delete(row)
    await db.commit()
    return ok({"id": feedback_id, "deleted": True}, "删除成功")


__all__ = ["router"]
