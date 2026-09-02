# -*- coding: utf-8 -*-
"""新品排名：按记录评分排序，规则可由后续采集任务持续写入。"""
from typing import Any
from fastapi import APIRouter, Body, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.db.session import get_session
from common.models import RankingEntry
from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.api.routes.generic import serialize_model, _user_id

router = APIRouter(prefix="/api/v1/ranking", tags=["新品排名"])


@router.get("")
@router.get("/")
async def list_ranking(user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(RankingEntry).where(RankingEntry.owner_id == _user_id(user)).order_by(RankingEntry.score.desc(), RankingEntry.id.desc()).limit(100))
    items = [serialize_model(item) for item in result.scalars().all()]
    return ok({"items": items, "total": len(items), "rule": "按 score 降序"})


@router.post("")
@router.post("/")
async def create_ranking(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    data = payload or {}
    item = RankingEntry(owner_id=_user_id(user), title=str(data.get("title", "待评估新品")), category=data.get("category"), score=float(data.get("score", 0)), source=data.get("source"))
    session.add(item); await session.commit(); await session.refresh(item)
    return ok(serialize_model(item), "排名记录已添加")
