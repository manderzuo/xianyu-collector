# -*- coding: utf-8 -*-
"""分销卡券上游货源、库存和提货接口。"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.services.card_dock import get_card_secret_key, purchase_url, request_upstream
from common.db.session import get_session

router = APIRouter(prefix="/api/v1/card-dock", tags=["分销卡券"])
_purchase_timestamps: dict[int, list[float]] = {}
_PURCHASE_LIMIT = 5
_PURCHASE_WINDOW = 60.0


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _rate_allowed(user_id: int) -> bool:
    now = time.time()
    values = [item for item in _purchase_timestamps.get(user_id, []) if item > now - _PURCHASE_WINDOW]
    if len(values) >= _PURCHASE_LIMIT:
        _purchase_timestamps[user_id] = values
        return False
    values.append(now)
    _purchase_timestamps[user_id] = values
    return True


def _source(value: str) -> str:
    return str(value or "").strip()


@router.get("/sources")
async def list_sources(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return await request_upstream(_uid(user), db, "GET", "/sources")


@router.get("/goods")
async def list_goods(
    source_code: str = Query(""), page: int = Query(1, ge=1), per_page: int = Query(15, ge=1, le=100), search: str = Query(""),
    user=Depends(get_current_user), db: AsyncSession = Depends(get_session),
):
    source_code = _source(source_code)
    if not source_code:
        return {"success": False, "code": 400, "message": "请先选择卡券商", "data": None}
    return await request_upstream(_uid(user), db, "GET", "/goods", params={"source_code": source_code, "page": page, "per_page": per_page, "search": search.strip()})


@router.get("/goods/{goods_id}")
async def goods_detail(goods_id: int, source_code: str = Query(""), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    source_code = _source(source_code)
    if not source_code:
        return {"success": False, "code": 400, "message": "请先选择卡券商", "data": None}
    return await request_upstream(_uid(user), db, "GET", f"/goods/{goods_id}", params={"source_code": source_code})


@router.get("/goods/{goods_id}/stock")
async def goods_stock(goods_id: int, source_code: str = Query(""), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    source_code = _source(source_code)
    if not source_code:
        return {"success": False, "code": 400, "message": "请先选择卡券商", "data": None}
    return await request_upstream(_uid(user), db, "GET", f"/goods/{goods_id}/stock", params={"source_code": source_code})


@router.post("/purchase")
async def purchase(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    user_id = _uid(user)
    if not _rate_allowed(user_id):
        return {"success": False, "code": 429, "message": "请求过于频繁，请稍后再试", "data": None}
    source_code = _source(payload.get("source_code"))
    if not source_code:
        return {"success": False, "code": 400, "message": "请先选择卡券商", "data": None}
    try:
        goods_id = int(payload.get("goods_id") or 0)
        sub_id = int(payload.get("sub_id") or 0)
        quantity = int(payload.get("quantity") or 1)
    except (TypeError, ValueError):
        return {"success": False, "code": 422, "message": "商品、规格或数量参数无效", "data": None}
    if goods_id <= 0 or sub_id <= 0 or quantity < 1:
        return {"success": False, "code": 400, "message": "商品、规格或数量参数无效", "data": None}
    return await request_upstream(user_id, db, "POST", "/purchase", body={"source_code": source_code, "goods_id": goods_id, "sub_id": sub_id, "quantity": quantity})


@router.get("/purchase-url")
async def get_purchase_url(
    source_code: str = Query(""), goods_id: int = Query(0, ge=0), sub_id: int = Query(0, ge=0), quantity: int = Query(1, ge=1),
    user=Depends(get_current_user), db: AsyncSession = Depends(get_session),
):
    source_code = _source(source_code)
    if not source_code:
        return {"success": False, "code": 400, "message": "请先选择卡券商", "data": None}
    if goods_id <= 0 or sub_id <= 0:
        return {"success": False, "code": 400, "message": "商品或规格参数无效", "data": None}
    secret_key = await get_card_secret_key(_uid(user), db)
    if not secret_key:
        return {"success": False, "code": 400, "message": "尚未配置对接卡密秘钥，请前往个人设置-分销管理填写", "data": None}
    return ok({"url": purchase_url(source_code, goods_id, sub_id, quantity, secret_key)}, "提货地址生成成功")
