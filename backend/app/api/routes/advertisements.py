# -*- coding: utf-8 -*-
"""广告申请、审核和公开展示的持久化接口。"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models import Advertisement, SystemSetting

router = APIRouter(prefix="/api/v1/advertisements", tags=["广告管理"])


def _uid(user: dict) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _is_admin(user: dict) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _serialize(row: Advertisement) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "title": row.title,
        "content": row.content,
        "link": row.link,
        "expire_date": row.expire_date.isoformat() if row.expire_date else None,
        "image_url": row.image_url,
        "ad_type": row.ad_type,
        "months": row.months,
        "total_amount": row.total_amount,
        "status": row.status,
        "source": "local",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _unit_price(db: AsyncSession, ad_type: str) -> Decimal | None:
    key = f"ad_price.{ad_type}"
    value = (
        await db.execute(
            select(SystemSetting.setting_value).where(SystemSetting.setting_key == key).limit(1)
        )
    ).scalar_one_or_none()
    if value is None or not str(value).strip():
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise HTTPException(422, f"{ad_type}广告价格配置异常，请联系管理员")


def _add_months(start: date, months: int) -> date:
    index = start.year * 12 + start.month - 1 + months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def _validate_type(ad_type: str) -> str:
    value = str(ad_type or "").strip().lower()
    if value not in {"carousel", "text"}:
        raise HTTPException(422, "广告类型仅支持 carousel 或 text")
    return value


def _validate_months(months: int) -> int:
    if months < 1 or months > 120:
        raise HTTPException(422, "广告月数必须在 1 到 120 之间")
    return months


async def _list(db: AsyncSession, statement, page: int, page_size: int) -> dict:
    total = int((await db.execute(select(func.count()).select_from(statement.subquery()))).scalar_one() or 0)
    rows = list((await db.execute(
        statement.order_by(desc(Advertisement.created_at)).offset((page - 1) * page_size).limit(page_size)
    )).scalars().all())
    return {"items": [_serialize(row) for row in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/public")
async def public_ads(db: AsyncSession = Depends(get_session)):
    today = date.today()
    rows = list((await db.execute(
        select(Advertisement)
        .where(
            Advertisement.status == "approved",
            (Advertisement.expire_date.is_(None) | (Advertisement.expire_date >= today)),
        )
        .order_by(desc(Advertisement.created_at))
        .limit(100)
    )).scalars().all())
    return ok({
        "carousel": [_serialize(row) for row in rows if row.ad_type == "carousel"],
        "text": [_serialize(row) for row in rows if row.ad_type == "text"],
    }, "查询成功")


@router.get("/prices")
async def ad_prices(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    del user
    prices: dict[str, str] = {}
    for ad_type in ("carousel", "text"):
        price = await _unit_price(db, ad_type)
        prices[ad_type] = str(price) if price is not None else "0"
    return ok(prices, "广告价格查询成功")


@router.get("/admin")
async def all_ads(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = None,
    ad_type: str | None = None,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    if not _is_admin(user):
        raise HTTPException(403, "仅管理员可以查看全部广告")
    statement = select(Advertisement)
    if status:
        statement = statement.where(Advertisement.status == status)
    if ad_type:
        statement = statement.where(Advertisement.ad_type == _validate_type(ad_type))
    return ok(await _list(db, statement, page, page_size), "广告查询成功")


@router.get("")
async def my_ads(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    return ok(await _list(db, select(Advertisement).where(Advertisement.user_id == _uid(user)), page, page_size), "广告查询成功")


@router.post("")
async def create_ad(
    title: str = Query(..., min_length=1, max_length=255),
    ad_type: str = Query(...),
    months: int = Query(..., ge=1, le=120),
    content: str | None = None,
    link: str | None = None,
    image_url: str | None = None,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    ad_type = _validate_type(ad_type)
    months = _validate_months(months)
    unit_price = await _unit_price(db, ad_type)
    if unit_price is None:
        raise HTTPException(422, f"{ad_type}广告尚未配置单月价格，请先在系统设置中配置")
    total = unit_price * months
    row = Advertisement(
        user_id=_uid(user), title=escape(title.strip()), content=escape(content or "") or None,
        link=(link.strip() or None) if link else None, image_url=(image_url.strip() or None) if image_url else None,
        ad_type=ad_type, months=months, total_amount=str(total), expire_date=_add_months(date.today(), months), status="unpaid",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return ok({"id": row.id, "total_amount": row.total_amount, "expire_date": row.expire_date.isoformat()}, "广告申请已提交")


@router.put("/admin/{ad_id}/approve")
async def approve_ad(ad_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    if not _is_admin(user):
        raise HTTPException(403, "仅管理员可以审核广告")
    row = await db.get(Advertisement, ad_id)
    if row is None:
        raise HTTPException(404, "广告不存在")
    row.status = "approved"
    await db.commit()
    return ok({"id": row.id, "status": row.status}, "广告已通过审核")


@router.put("/admin/{ad_id}/reject")
async def reject_ad(ad_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    if not _is_admin(user):
        raise HTTPException(403, "仅管理员可以审核广告")
    row = await db.get(Advertisement, ad_id)
    if row is None:
        raise HTTPException(404, "广告不存在")
    row.status = "pending"
    await db.commit()
    return ok({"id": row.id, "status": row.status}, "广告已退回复核状态")


@router.put("/admin/{ad_id}")
async def update_ad_admin(
    ad_id: int,
    title: str = Query(..., min_length=1, max_length=255),
    ad_type: str = Query(...),
    content: str | None = None,
    link: str | None = None,
    expire_date: str | None = None,
    image_url: str | None = None,
    status: str | None = None,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    if not _is_admin(user):
        raise HTTPException(403, "仅管理员可以修改广告")
    row = await db.get(Advertisement, ad_id)
    if row is None:
        raise HTTPException(404, "广告不存在")
    row.title = escape(title.strip())
    row.content = escape(content or "") or None
    row.link = (link.strip() or None) if link else None
    row.image_url = (image_url.strip() or None) if image_url else None
    row.ad_type = _validate_type(ad_type)
    if expire_date:
        try:
            row.expire_date = date.fromisoformat(expire_date)
        except ValueError as exc:
            raise HTTPException(422, "到期日期格式无效") from exc
    else:
        row.expire_date = None
    if status:
        if status not in {"unpaid", "pending", "approved"}:
            raise HTTPException(422, "广告状态无效")
        row.status = status
    await db.commit()
    return ok(_serialize(row), "广告已更新")


@router.delete("/admin/{ad_id}")
async def delete_ad_admin(ad_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    if not _is_admin(user):
        raise HTTPException(403, "仅管理员可以删除广告")
    row = await db.get(Advertisement, ad_id)
    if row is None:
        raise HTTPException(404, "广告不存在")
    await db.delete(row)
    await db.commit()
    return ok({"id": ad_id, "deleted": True}, "广告已删除")


@router.put("/{ad_id}")
async def update_ad(
    ad_id: int,
    title: str = Query(..., min_length=1, max_length=255),
    ad_type: str = Query(...),
    months: int = Query(..., ge=1, le=120),
    content: str | None = None,
    link: str | None = None,
    image_url: str | None = None,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    row = (await db.execute(select(Advertisement).where(Advertisement.id == ad_id, Advertisement.user_id == _uid(user)))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "广告不存在或无权修改")
    if row.status == "approved":
        raise HTTPException(409, "已审核广告不能直接修改")
    ad_type = _validate_type(ad_type)
    months = _validate_months(months)
    unit_price = await _unit_price(db, ad_type)
    if unit_price is None:
        raise HTTPException(422, f"{ad_type}广告尚未配置单月价格，请先在系统设置中配置")
    row.title = escape(title.strip()); row.content = escape(content or "") or None
    row.link = (link.strip() or None) if link else None; row.image_url = (image_url.strip() or None) if image_url else None
    row.ad_type = ad_type; row.months = months; row.total_amount = str(unit_price * months)
    row.expire_date = _add_months(date.today(), months); row.status = "unpaid"
    await db.commit()
    return ok(_serialize(row), "广告已更新")


@router.delete("/{ad_id}")
async def delete_ad(ad_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(Advertisement).where(Advertisement.id == ad_id, Advertisement.user_id == _uid(user)))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "广告不存在或无权删除")
    await db.delete(row)
    await db.commit()
    return ok({"id": ad_id, "deleted": True}, "广告已删除")


@router.post("/{ad_id}/pay")
async def create_ad_payment(ad_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    """创建付款占位只在支付通道已配置时允许继续，禁止伪造支付成功。"""
    row = (await db.execute(select(Advertisement).where(Advertisement.id == ad_id, Advertisement.user_id == _uid(user)))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "广告不存在")
    raise HTTPException(503, "广告支付通道尚未配置，广告仍保持待付款状态")


@router.post("/{ad_id}/pay/notify")
async def check_ad_payment(ad_id: int, order_no: str = Query(...), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(Advertisement).where(Advertisement.id == ad_id, Advertisement.user_id == _uid(user)))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "广告不存在")
    return ok({"status": row.status, "order_no": order_no}, "支付状态已查询；未配置支付回调时不会自动审核")
