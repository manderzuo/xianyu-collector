# -*- coding: utf-8 -*-
"""卡券管理接口。

卡券没有独立旧表时使用 FeatureRecord 作为结构化持久化载体，但所有
增删改查和商品关联都在这里完成，不再经过通用兼容兜底接口。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.services.entitlements import FEATURE_CARD_AUTO_DELIVERY, finalize_quota, reserve_quota
from common.config import settings
from common.db.session import get_session
from common.models import FeatureRecord

router = APIRouter(prefix="/api/v1/cards", tags=["卡券管理"])
FEATURE = "cards"


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _statement(user: dict[str, Any]):
    return select(FeatureRecord).where(FeatureRecord.feature == FEATURE, FeatureRecord.owner_id == _uid(user))


def _serialize(item: FeatureRecord) -> dict[str, Any]:
    payload = dict(item.payload or {})
    payload.update({"id": item.id, "status": item.status, "created_at": item.created_at, "updated_at": item.updated_at})
    payload.setdefault("enabled", item.status not in {"disabled", "inactive", "deleted"})
    return payload


def _matches(card: dict[str, Any], search: str | None, card_type: str | None, item_id: str | None = None) -> bool:
    if card_type and str(card.get("type") or "") != card_type:
        return False
    if search:
        haystack = " ".join(str(card.get(key) or "") for key in ("name", "description", "text_content", "data_content"))
        if search.lower() not in haystack.lower():
            return False
    if item_id:
        item_ids = {str(value) for value in (card.get("item_ids") or [])}
        if card.get("item_id"):
            item_ids.add(str(card["item_id"]))
        if str(item_id) not in item_ids:
            return False
    return True


async def _get_card(card_id: int, user: dict[str, Any], db: AsyncSession) -> FeatureRecord:
    item = (await db.execute(_statement(user).where(FeatureRecord.id == card_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="卡券不存在")
    return item


@router.get("")
@router.get("/")
async def list_cards(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=10000),
    search: str | None = Query(None),
    type: str | None = Query(None),
    lite: int = Query(0, ge=0, le=1),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    rows = (await db.execute(_statement(user).order_by(FeatureRecord.id.desc()))).scalars().all()
    cards = [_serialize(row) for row in rows if _matches(_serialize(row), search, type)]
    total = len(cards)
    page_rows = cards[(page - 1) * page_size: page * page_size]
    if lite:
        page_rows = [{key: value for key, value in card.items() if key not in {"api_config", "text_content", "data_content", "image_url", "image_urls"}} for card in page_rows]
    return ok({
        "list": page_rows,
        "items": page_rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if total else 0,
    }, "卡券查询成功")


@router.post("")
@router.post("/")
async def create_card(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    data = dict(payload or {})
    if not str(data.get("name") or "").strip():
        raise HTTPException(status_code=422, detail="卡券名称不能为空")
    external_id = uuid4().hex
    reservation = None
    if bool(data.get("enabled", True)):
        reservation = await reserve_quota(
            db,
            user,
            FEATURE_CARD_AUTO_DELIVERY,
            resource_key=f"card:{external_id}",
            idempotency_key=f"card:create:{external_id}",
        )
    item = FeatureRecord(
        owner_id=_uid(user),
        feature=FEATURE,
        external_id=external_id,
        status="active" if data.get("enabled", True) else "disabled",
        payload=data,
    )
    db.add(item)
    finalize_quota(reservation)
    await db.commit()
    await db.refresh(item)
    return ok(_serialize(item), "卡券创建成功")


@router.get("/selectable")
async def selectable_cards(
    item_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=10000),
    search: str | None = Query(None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    rows = (await db.execute(_statement(user).order_by(FeatureRecord.id.desc()))).scalars().all()
    cards = []
    for row in rows:
        card = _serialize(row)
        if not _matches(card, search, None, item_id):
            continue
        cards.append({
            "id": card.get("id"), "name": card.get("name", ""), "type": card.get("type", "text"),
            "source": "own", "unique_key": f"own:{card.get('id')}", "enabled": card.get("enabled", True),
            "price": card.get("price"), "is_multi_spec": card.get("is_multi_spec", False),
        })
    total = len(cards)
    return ok({"list": cards[(page - 1) * page_size: page * page_size], "total": total, "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size if total else 0}, "卡券查询成功")


@router.get("/selectable/all")
async def selectable_all(search: str | None = Query(None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    result = await selectable_cards(item_id=None, page=1, page_size=10000, search=search, user=user, db=db)
    return result


@router.get("/item/{item_id}")
async def cards_by_item(item_id: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = (await db.execute(_statement(user).order_by(FeatureRecord.id.desc()))).scalars().all()
    cards = [_serialize(row) for row in rows if _matches(_serialize(row), None, None, item_id)]
    return ok(cards, "商品卡券查询成功")


@router.get("/{card_id}/items")
async def get_card_items(card_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    card = await _get_card(card_id, user, db)
    payload = dict(card.payload or {})
    item_ids = [str(item) for item in (payload.get("item_ids") or [])]
    if payload.get("item_id") and str(payload["item_id"]) not in item_ids:
        item_ids.append(str(payload["item_id"]))
    return ok({"item_ids": item_ids}, "卡券关联商品查询成功")


@router.get("/{card_id}")
async def get_card(card_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return ok(_serialize(await _get_card(card_id, user, db)), "卡券查询成功")


@router.put("/item/{item_id}/cards")
async def update_item_cards(item_id: str, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    relations = (payload or {}).get("card_items") or []
    card_ids = {int(value.get("card_id")) for value in relations if isinstance(value, dict) and str(value.get("card_id", "")).isdigit()}
    rows = (await db.execute(_statement(user))).scalars().all()
    for row in rows:
        data = dict(row.payload or {})
        ids = {str(value) for value in (data.get("item_ids") or [])}
        if row.id in card_ids:
            ids.add(str(item_id))
        else:
            ids.discard(str(item_id))
        data["item_ids"] = sorted(ids)
        data["item_id"] = str(item_id) if row.id in card_ids else (next(iter(ids), "") or None)
        row.payload = data
    await db.commit()
    return ok({"item_id": item_id, "card_ids": sorted(card_ids)}, "商品卡券关联已保存")


@router.put("/{card_id}/items")
async def update_card_items(card_id: int, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    card = await _get_card(card_id, user, db)
    data = dict(card.payload or {})
    item_ids = [str(value) for value in ((payload or {}).get("item_ids") or []) if str(value).strip()]
    data["item_ids"] = list(dict.fromkeys(item_ids))
    data["item_id"] = item_ids[0] if item_ids else None
    card.payload = data
    await db.commit()
    # AsyncSession 默认会在 commit 后使对象属性过期；直接序列化 card
    # 会触发隐式懒加载，在异步上下文中导致 MissingGreenlet（接口 500）。
    # 显式刷新后再序列化，确保保存成功响应稳定返回。
    await db.refresh(card)
    return ok(_serialize(card), "卡券关联商品已保存")


@router.put("/{card_id}")
async def update_card(card_id: int, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    card = await _get_card(card_id, user, db)
    data = dict(card.payload or {})
    was_enabled = card.status == "active" and bool(data.get("enabled", True))
    data.update(payload or {})
    reservation = None
    if bool(data.get("enabled", True)) and not was_enabled:
        reservation = await reserve_quota(
            db,
            user,
            FEATURE_CARD_AUTO_DELIVERY,
            resource_key=f"card:{card_id}",
            idempotency_key=f"card:enable:{card_id}:{card.updated_at or card_id}",
        )
    if "enabled" in data:
        card.status = "active" if data["enabled"] else "disabled"
    card.payload = data
    finalize_quota(reservation)
    await db.commit()
    await db.refresh(card)
    return ok(_serialize(card), "卡券更新成功")


@router.delete("/{card_id}")
async def delete_card(card_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    card = await _get_card(card_id, user, db)
    await db.delete(card)
    await db.commit()
    return ok({"id": card_id, "deleted": True}, "卡券已删除")


@router.post("/batch-delete")
async def batch_delete_cards(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    ids = [int(value) for value in ((payload or {}).get("ids") or []) if str(value).isdigit()]
    if ids:
        await db.execute(delete(FeatureRecord).where(FeatureRecord.feature == FEATURE, FeatureRecord.owner_id == _uid(user), FeatureRecord.id.in_(ids)))
        await db.commit()
    return ok({"deleted": len(ids), "ids": ids}, "卡券已批量删除")


@router.post("/upload-image")
async def upload_card_image(image: UploadFile = File(...), user=Depends(get_current_user)):
    upload_dir = Path(settings.static_dir) / "uploads" / "cards"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(image.filename or "card-image.bin").name
    target = upload_dir / f"{uuid4().hex}_{safe_name}"
    target.write_bytes(await image.read())
    return ok({"image_url": f"/static/uploads/cards/{target.name}"}, "卡券图片已上传")


@router.post("/batch-clear-item-relations")
async def batch_clear_item_relations(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item_ids = {str(value) for value in ((payload or {}).get("item_ids") or [])}
    rows = (await db.execute(_statement(user))).scalars().all()
    for row in rows:
        data = dict(row.payload or {})
        data["item_ids"] = [str(value) for value in (data.get("item_ids") or []) if str(value) not in item_ids]
        if str(data.get("item_id") or "") in item_ids:
            data["item_id"] = data["item_ids"][0] if data["item_ids"] else None
        row.payload = data
    await db.commit()
    return ok({"item_ids": sorted(item_ids)}, "商品卡券关联已清空")
