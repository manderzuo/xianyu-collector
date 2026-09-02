"""基于已登录账号的闲鱼商品搜索。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.db.session import get_session
from common.models import Account
from common.services.goofish_client import GoofishClient

router = APIRouter(prefix="/api/v1/search", tags=["商品搜索"])


class ItemSearchRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=100)
    page: int = Field(default=1, ge=1, le=100)
    page_size: int = Field(default=20, ge=1, le=50)
    account_id: int | None = None


@router.post("/items")
async def search_items(payload: ItemSearchRequest, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    uid = int(user.get("sub", 1))
    statement = select(Account).where(Account.user_id == uid, Account.status == "active", Account.cookie.is_not(None))
    if payload.account_id is not None:
        statement = statement.where(Account.id == payload.account_id)
    account = (await db.execute(statement.order_by(Account.id.desc()))).scalars().first()
    if account is None or not account.cookie:
        raise HTTPException(status_code=409, detail="没有可用的闲鱼登录态，请先扫码登录")
    try:
        result = await GoofishClient(account.cookie, account.proxy).search(payload.keyword, payload.page, payload.page_size)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"闲鱼搜索失败：{exc}") from exc
    return ok({"items": result["items"], "total": result["total"], "page": payload.page, "page_size": payload.page_size, "account_id": account.id}, "搜索成功")
