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
    """通知连接服务加载新登录态，并等待首次 Token/连接验证结果。"""
    action = "start" if is_new else "restart"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=12, write=10, pool=12)) as client:
            response = await client.post(
                f"{settings.websocket_service_url.rstrip('/')}/internal/accounts/{account.id}/{action}",
                json={"cookie_value": cookie_value, "user_id": user_id},
            )
        payload = response.json()
        if not response.is_success or not payload.get("success"):
            return {"status": "failed", "action": action, "detail": payload.get("message", "连接服务拒绝请求")}
        return {
            "status": "pending",
            "action": action,
            "detail": payload.get("data") or {},
            "message": "登录态已保存，正在验证闲鱼 Token 和长连接",
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {"status": "unavailable", "action": action, "detail": str(exc)}


async def _get_runtime_status(account_id: int) -> dict:
    """查询真实运行时状态；任务启动不等于账号已在线。"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=8, write=5, pool=8)) as client:
            response = await client.get(
                f"{settings.websocket_service_url.rstrip('/')}/internal/accounts/{account_id}/status"
            )
        payload = response.json()
        if response.is_success and payload.get("success"):
            return {"status": "ok", "detail": payload.get("data") or {}}
        return {"status": "failed", "detail": payload.get("message", "连接服务状态查询失败")}
    except (httpx.HTTPError, ValueError) as exc:
        return {"status": "unavailable", "detail": str(exc)}


def _runtime_error_message(runtime: dict) -> str:
    detail = runtime.get("detail") if isinstance(runtime, dict) else ""
    if isinstance(detail, dict):
        error = str(detail.get("last_error") or "").strip()
    else:
        error = str(detail or "").strip()
    upper = error.upper()
    if "USER_VALIDATE" in upper or "ILLEGAL_ACCESS" in upper:
        return (
            "闲鱼要求完成设备安全验证，扫码登录未完成。"
            "请先在闲鱼/淘宝客户端或浏览器完成验证后重新扫码，"
            "或在系统设置中配置远程 Token。"
        )
    return error[:500] or "后台连接服务未能完成登录验证"


async def _complete_runtime_login(item: QrLoginSession, db: AsyncSession) -> dict | None:
    """把已保存 Cookie 的扫码会话收口为 success/failed/processing。"""
    if item.account_id is None:
        return None
    runtime = await _get_runtime_status(int(item.account_id))
    if runtime.get("status") != "ok":
        return {"status": "processing", "runtime": runtime, "message": "正在等待连接服务完成登录验证"}
    detail = runtime.get("detail") or {}
    connection_state = str(detail.get("connection_state") or "")
    if connection_state == "connected" and detail.get("is_connected"):
        item.status = "success"
        item.error = None
        await db.commit()
        return {"status": "success", "runtime": runtime, "message": "扫码登录成功，账号已在线"}
    last_error = str(detail.get("last_error") or "")
    error_upper = last_error.upper()
    risk_or_manual = (
        "USER_VALIDATE" in error_upper
        or "ILLEGAL_ACCESS" in error_upper
        or "设备安全验证" in last_error
        or "Cookie为空" in last_error
    )
    if connection_state in {"expired", "failed", "closed"} or risk_or_manual:
        item.status = "failed"
        item.error = _runtime_error_message(runtime)
        account = await db.get(Account, int(item.account_id))
        if account is not None:
            account.status = "expired"
            account.cookie_expire_at = _now()
        await db.commit()
        return {"status": "failed", "runtime": runtime, "message": item.error}
    return {"status": "processing", "runtime": runtime, "message": "登录态已保存，正在验证 Token 和长连接"}


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
        if item.account_id is not None and item.status in {"success", "failed"}:
            message = "扫码登录成功，账号已在线" if item.status == "success" else (item.error or "扫码登录失败")
            return ok(
                {**_serialize_session(item, include_qr=False), "account_info": {"account_id": item.account_id, "is_new_account": bool(item.is_new_account)}},
                message,
            )
        if item.account_id is not None and item.status == "processing":
            result = await _complete_runtime_login(item, db)
            if result is not None:
                await db.refresh(item)
                message = str(result.get("message") or "正在验证登录态")
                return ok(
                    {
                        **_serialize_session(item, include_qr=False),
                        "account_info": {"account_id": item.account_id, "is_new_account": bool(item.is_new_account)},
                        "runtime": result.get("runtime"),
                    },
                    message,
                )

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
                login_expire_at = _now() + timedelta(days=30)
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
                            cookie_expire_at=login_expire_at,
                        )
                        db.add(account)
                        await db.flush()
                    else:
                        account = existing
                        account.cookie = cookie_value
                        account.status = "active"
                        account.cookie_expire_at = login_expire_at
                        if nickname and is_generated_account_name(account.account_name, account.goofish_id):
                            account.account_name = nickname
                    db.add(AccountCookie(account_id=account.id, cookie_value=cookie_value, status="active", expires_at=login_expire_at))
                    item.cookie_value = cookie_value
                    item.account_id = account.id
                    item.is_new_account = is_new
                    # Cookie 落库只代表扫码确认完成；必须等 Token 和 IM 长连接
                    # 验证通过后，才能把扫码会话标记为真正成功。
                    item.status = "processing"
                    await db.commit()
                    runtime = await _notify_account_runtime(account, cookie_value, _uid(user), is_new)
                    if runtime.get("status") in {"failed", "unavailable"}:
                        item.status = "failed"
                        item.error = _runtime_error_message(runtime)
                        account.status = "expired"
                        account.cookie_expire_at = _now()
                        await db.commit()
                        return ok(
                            {**_serialize_session(item, include_qr=False), "account_info": {"account_id": account.id, "is_new_account": is_new}, "runtime": runtime},
                            item.error,
                        )
                    result = await _complete_runtime_login(item, db)
                    await db.refresh(item)
                    if result and result.get("status") == "success":
                        return ok(
                            {**_serialize_session(item, include_qr=False), "account_info": {"account_id": account.id, "is_new_account": is_new}, "runtime": result.get("runtime")},
                            str(result.get("message") or "扫码登录成功，账号已在线"),
                        )
                    return ok(
                        {**_serialize_session(item, include_qr=False), "account_info": {"account_id": account.id, "is_new_account": is_new}, "runtime": (result or {}).get("runtime", runtime)},
                        str((result or {}).get("message") or "登录态已保存，正在验证 Token 和长连接"),
                    )

        await db.commit()
        await db.refresh(item)
        message = "查询成功"
        if item.status == "verification_required":
            message = "需要完成手机人脸核验"
        elif item.status == "scanned":
            message = "已扫码，请在手机上确认登录"
        elif item.status == "processing":
            message = "登录态已保存，正在验证 Token 和长连接"
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
