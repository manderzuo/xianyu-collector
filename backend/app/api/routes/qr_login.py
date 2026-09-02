"""真实闲鱼扫码登录接口。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.config import settings
from common.db.session import get_session
from common.models import Account, AccountCookie, QrLoginSession
from common.services.account_identity import extract_account_nickname, is_generated_account_name
from backend.app.services.qr_login import qr_login_manager

router = APIRouter(prefix="/api/v1/qr-login", tags=["真实扫码登录"])
_session_locks: dict[str, asyncio.Lock] = {}


class QRGenerateRequest(BaseModel):
    proxy: str | None = Field(default=None, max_length=512)


def _uid(user: dict) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _lock_for(session_id: str) -> asyncio.Lock:
    return _session_locks.setdefault(session_id, asyncio.Lock())


def _serialize_session(item: QrLoginSession, *, include_qr: bool = True) -> dict:
    data = {
        "session_id": item.session_id,
        "status": item.status,
        "unb": item.unb,
        "account_id": item.account_id,
        "is_new_account": item.is_new_account,
        "verification_url": item.verification_url,
        "face_qr_url": item.face_qr_url,
        "error": item.error,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }
    if include_qr:
        data["qr_code_url"] = item.qr_code_url
    return data


async def _get_owned_session(session_id: str, owner_id: int, db: AsyncSession) -> QrLoginSession:
    item = (
        await db.execute(
            select(QrLoginSession).where(
                QrLoginSession.session_id == session_id,
                QrLoginSession.owner_id == owner_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="扫码会话不存在或不属于当前用户")
    return item


async def _notify_account_runtime(account: Account, cookie_value: str, user_id: int, is_new: bool) -> dict:
    """通知连接服务加载新登录态；服务不可用时保留明确的运行状态。"""
    action = "start" if is_new else "restart"
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(
                f"{settings.websocket_service_url}/internal/accounts/{account.id}/{action}",
                json={"cookie_value": cookie_value, "user_id": user_id},
            )
        payload = response.json()
        if response.is_success and payload.get("success"):
            return {"status": "accepted", "action": action, "detail": payload.get("data")}
        return {"status": "failed", "action": action, "detail": payload.get("message", "连接服务拒绝请求")}
    except (httpx.HTTPError, ValueError) as exc:
        return {"status": "unavailable", "action": action, "detail": str(exc)}


@router.post("/generate")
async def generate_qr_code(
    payload: QRGenerateRequest | None = Body(default=None),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """向闲鱼登录服务申请二维码，并启动后台状态轮询。"""
    qr_login_manager.cleanup()
    result = await qr_login_manager.generate_qr_code((payload or QRGenerateRequest()).proxy)
    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result.get("message", "生成二维码失败"))
    item = QrLoginSession(
        owner_id=_uid(user),
        session_id=result["session_id"],
        status="waiting",
        qr_code_url=result.get("qr_code_url"),
        qr_content=qr_login_manager.sessions[result["session_id"]].qr_content,
        expires_at=_now() + timedelta(seconds=int(result.get("expires_in", 300))),
    )
    db.add(item)
    try:
        await db.commit()
        await db.refresh(item)
    except SQLAlchemyError as exc:
        await db.rollback()
        qr_login_manager.cancel(item.session_id)
        raise HTTPException(status_code=409, detail="扫码会话保存失败") from exc
    return ok(_serialize_session(item), "二维码生成成功")


@router.get("/status/{session_id}")
async def get_qr_status(
    session_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """查询扫码状态；成功后自动创建/更新账号并保存 Cookie。"""
    item = await _get_owned_session(session_id, _uid(user), db)
    async with _lock_for(session_id):
        if item.account_id is not None and item.status == "success":
            return ok({**_serialize_session(item, include_qr=False), "account_info": {"account_id": item.account_id, "is_new_account": bool(item.is_new_account)}}, "扫码登录成功")

        state = qr_login_manager.status(session_id)
        item.status = str(state.get("status", "not_found"))
        item.verification_url = state.get("verification_url")
        item.face_qr_url = state.get("face_qr_url")
        item.error = state.get("error")
        if item.status in {"expired", "cancelled", "not_found"}:
            item.expires_at = _now()

        if item.status == "success":
            cookies = qr_login_manager.cookies(session_id)
            if not cookies or not cookies.get("cookies"):
                item.status = "failed"
                item.error = "闲鱼已确认登录，但没有返回 Cookie"
            else:
                cookie_value = cookies["cookies"]
                unb = cookies.get("unb") or None
                nickname = str(cookies.get("nickname") or "").strip() or extract_account_nickname(cookie_value)
                existing = None
                if unb:
                    existing = (
                        await db.execute(select(Account).where(Account.goofish_id == unb))
                    ).scalar_one_or_none()
                    if existing and existing.user_id != _uid(user):
                        item.status = "failed"
                        item.error = "该闲鱼账号已经绑定到其他用户"
                    else:
                        item.unb = unb
                if item.status == "success":
                    is_new = existing is None
                    if existing is None:
                        account = Account(
                            user_id=_uid(user),
                            account_name=nickname or f"闲鱼账号-{unb or session_id[:8]}",
                            goofish_id=unb,
                            cookie=cookie_value,
                            status="active",
                        )
                        db.add(account)
                        await db.flush()
                    else:
                        account = existing
                        account.cookie = cookie_value
                        account.status = "active"
                        if nickname and is_generated_account_name(account.account_name, account.goofish_id):
                            account.account_name = nickname
                    db.add(AccountCookie(account_id=account.id, cookie_value=cookie_value, status="active"))
                    item.cookie_value = cookie_value
                    item.account_id = account.id
                    item.is_new_account = is_new
                    item.status = "success"
                    await db.commit()
                    runtime = await _notify_account_runtime(account, cookie_value, _uid(user), is_new)
                    return ok({**_serialize_session(item, include_qr=False), "account_info": {"account_id": account.id, "is_new_account": is_new}, "runtime": runtime}, "扫码登录成功")

        await db.commit()
        await db.refresh(item)
        message = "查询成功"
        if item.status == "verification_required":
            message = "需要完成手机人脸核验"
        elif item.status == "scanned":
            message = "已扫码，请在手机上确认登录"
        elif item.status == "expired":
            message = "二维码已过期，请重新生成"
        elif item.status == "failed":
            message = item.error or "扫码登录失败"
        return ok(_serialize_session(item, include_qr=False), message)


@router.get("/cookie/{session_id}")
async def get_qr_cookie(
    session_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    item = await _get_owned_session(session_id, _uid(user), db)
    if item.status != "success" or not item.cookie_value:
        raise HTTPException(status_code=409, detail="Cookie 不存在或会话尚未完成")
    return ok({"session_id": session_id, "cookies": item.cookie_value, "unb": item.unb}, "获取 Cookie 成功")


@router.post("/cancel/{session_id}")
async def cancel_qr_session(
    session_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    item = await _get_owned_session(session_id, _uid(user), db)
    qr_login_manager.cancel(session_id)
    item.status = "cancelled"
    await db.commit()
    return ok({"session_id": session_id, "status": "cancelled"}, "扫码会话已取消")
