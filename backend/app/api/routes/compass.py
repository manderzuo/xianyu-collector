# -*- coding: utf-8 -*-
"""闲鱼数据罗盘的即时搜索接口。

罗盘页面既可以查看定时采集结果，也保留了旧版的即时搜索入口。即时搜索
必须使用调用者选择的闲鱼账号 Cookie，搜索失败时把平台原始原因返回给前端，
不再把“等待连接”当成成功。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from common.db.session import get_session
from common.models import Account
from common.services.goofish_client import GoofishClient

router = APIRouter(prefix="/api/v1/compass/goofish", tags=["数据罗盘"])


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _compass_item(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    # 搜索接口当前稳定提供标题、价格、卖家、地区和链接；统计字段由不同
    # 版本的闲鱼卡片返回，按多个常见字段读取，缺失时明确为空而不是造数。
    extra = raw.get("data", {}).get("item", {}) if isinstance(raw.get("data"), dict) else {}
    extra = extra.get("main") if isinstance(extra, dict) else {}
    extra = extra if isinstance(extra, dict) else {}
    click_args = extra.get("clickParam", {}).get("args", {}) if isinstance(extra.get("clickParam"), dict) else {}
    return {
        "item_id": str(row.get("item_id") or ""),
        "title": str(row.get("title") or "未命名商品"),
        "price": str(row.get("price") or ""),
        "area": row.get("area"),
        "seller_name": row.get("seller") or extra.get("userNickName") or extra.get("userNick"),
        "item_url": row.get("url"),
        "main_image": extra.get("picUrl") or extra.get("mainImage") or click_args.get("picUrl"),
        "publish_time": extra.get("publishTime") or extra.get("gmtCreate"),
        "want_count": _number(extra.get("wantCount") or extra.get("wantNum") or click_args.get("wantCount")),
        "view_count": _number(extra.get("viewCount") or extra.get("browseCount") or click_args.get("viewCount")),
        "description": extra.get("description") or extra.get("desc"),
    }


def _number(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.post("/search")
async def compass_search(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    data = payload or {}
    keyword = str(data.get("keyword") or "").strip()
    if not keyword:
        raise HTTPException(status_code=422, detail="请输入搜索关键词")
    try:
        account_id = int(data.get("cookie_id") or data.get("account_id") or 0)
        start_page = max(1, min(int(data.get("start_page") or 1), 100))
        pages = max(1, min(int(data.get("pages") or 1), 10))
        page_size = max(1, min(int(data.get("page_size") or 20), 50))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="搜索分页参数无效") from exc
    account = (
        await db.execute(
            select(Account).where(Account.id == account_id, Account.user_id == _uid(user))
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="所选闲鱼账号不存在")
    if not (account.cookie or "").strip():
        raise HTTPException(status_code=409, detail="所选账号尚未登录，请先扫码登录")
    client = GoofishClient(account.cookie, account.proxy)
    items: list[dict[str, Any]] = []
    total_available = 0
    try:
        for page in range(start_page, start_page + pages):
            result = await client.search(keyword, page, page_size)
            total_available = max(total_available, int(result.get("total") or 0))
            items.extend(_compass_item(row) for row in result.get("items") or [] if row.get("item_id"))
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"闲鱼罗盘搜索失败：{str(exc)[:500]}") from exc
    return {
        "success": True,
        "items": items,
        "total": len(items),
        "total_available": total_available or len(items),
    }
