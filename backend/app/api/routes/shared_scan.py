"""多人协作扫码登录：会话、独立二维码、扫码结果交接和账号启动。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.qr_login import _uid as qr_uid
from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.services.qr_login import qr_login_manager
from common.config import settings
from common.db.session import get_session
from common.models import Account, AccountCookie, SharedScanSession, SharedScanWorker
from common.services.account_identity import extract_account_nickname, is_generated_account_name

router = APIRouter(prefix="/api/v1/shared-scan", tags=["共享扫码"])
_worker_locks: dict[str, asyncio.Lock] = {}
_session_locks: dict[str, asyncio.Lock] = {}


def _uid(user: dict) -> int:
    return qr_uid(user)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _lock(store: dict[str, asyncio.Lock], key: str) -> asyncio.Lock:
    return store.setdefault(key, asyncio.Lock())


def _share_url(request: Request, token: str) -> str:
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    if not origin:
        referer = (request.headers.get("referer") or "").strip()
        if "://" in referer:
            origin = referer.split("/", 3)[0:3]
            origin = "/".join(origin).rstrip("/")
    if not origin:
        origin = settings.backend_service_url.rstrip("/")
    return f"{origin}/shared-scan-page?session_id={token}"


def _session_data(item: SharedScanSession, request: Request | None = None, *, worker_count: int = 0, success_count: int = 0) -> dict:
    token = item.session_token
    return {
        "session_id": token,
        "status": item.status,
        "share_url": _share_url(request, token) if request else None,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "worker_count": worker_count,
        "success_count": success_count,
    }


def _worker_data(item: SharedScanWorker) -> dict:
    created = item.created_at.replace(tzinfo=timezone.utc) if item.created_at and item.created_at.tzinfo is None else item.created_at
    return {
        "sub_session_id": item.sub_session_id,
        "status": item.status,
        "account_id": item.account_id,
        "cookie_saved": bool(item.cookie_saved),
        "joined_at": int(created.timestamp()) if created else 0,
        "message": item.error,
    }


async def _owned_session(token: str, owner_id: int, db: AsyncSession) -> SharedScanSession:
    item = (await db.execute(select(SharedScanSession).where(SharedScanSession.session_token == token, SharedScanSession.owner_id == owner_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "共享扫码会话不存在或无权访问")
    if item.status in {"pending", "active"} and item.expires_at and item.expires_at < _now():
        item.status = "expired"
        await db.commit()
    return item


async def _active_session(token: str, db: AsyncSession) -> SharedScanSession:
    item = (await db.execute(select(SharedScanSession).where(SharedScanSession.session_token == token))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "共享扫码会话不存在")
    if item.status not in {"pending", "active"}:
        raise HTTPException(409, "共享扫码会话已关闭")
    if item.expires_at and item.expires_at < _now():
        item.status = "expired"
        await db.commit()
        raise HTTPException(409, "共享扫码会话已过期")
    if item.status == "pending":
        item.status = "active"
        await db.commit()
    return item


async def _notify_runtime(account: Account, cookie_value: str, user_id: int, is_new: bool) -> dict:
    action = "start" if is_new else "restart"
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(
                f"{settings.websocket_service_url.rstrip('/')}/internal/accounts/{account.id}/{action}",
                json={"cookie_value": cookie_value, "user_id": user_id},
            )
        payload = response.json()
        if response.is_success and payload.get("success"):
            return {"status": "accepted", "action": action, "detail": payload.get("data")}
        return {"status": "failed", "action": action, "detail": payload.get("message", "连接服务拒绝请求")}
    except (httpx.HTTPError, ValueError) as exc:
        return {"status": "unavailable", "action": action, "detail": str(exc)}


@router.post("/create")
async def create_session(request: Request, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = SharedScanSession(
        owner_id=_uid(user),
        session_token=uuid4().hex,
        status="active",
        expires_at=_now() + timedelta(hours=72),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return ok(_session_data(item, request), "共享会话创建成功")


@router.get("/list")
async def list_sessions(request: Request, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    sessions = list((await db.execute(select(SharedScanSession).where(SharedScanSession.owner_id == _uid(user)).order_by(SharedScanSession.id.desc()))).scalars().all())
    output = []
    for item in sessions:
        workers = list((await db.execute(select(SharedScanWorker).where(SharedScanWorker.shared_session_id == item.session_token))).scalars().all())
        output.append(_session_data(item, request, worker_count=len(workers), success_count=sum(1 for worker in workers if worker.status == "success")))
    return ok({"sessions": output, "items": output, "total": len(output)}, "共享会话查询成功")


@router.get("/status")
async def session_status(session_id: str = Query(...), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    session = await _owned_session(session_id, _uid(user), db)
    workers = list((await db.execute(select(SharedScanWorker).where(SharedScanWorker.shared_session_id == session.session_token).order_by(SharedScanWorker.id.asc()))).scalars().all())
    values = [_worker_data(item) for item in workers]
    return ok({"session_id": session.session_token, "session_status": session.status, "part_time_workers": values, "workers": values}, "共享会话状态查询成功")


@router.delete("/{session_id}")
async def delete_session(session_id: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    session = await _owned_session(session_id, _uid(user), db)
    await db.execute(delete(SharedScanWorker).where(SharedScanWorker.shared_session_id == session.session_token))
    await db.delete(session)
    await db.commit()
    return ok({"session_id": session_id, "deleted": True, "status": "cancelled"}, "共享扫码会话已删除")


@router.post("/join")
async def join_session(payload: dict, db: AsyncSession = Depends(get_session)):
    """公开加入入口：每个访客都拿到独立二维码，且不会要求管理端登录。"""
    token = str(payload.get("session_id") or payload.get("session_token") or payload.get("token") or "").strip()
    if not token:
        raise HTTPException(422, "缺少共享会话令牌")
    force_refresh = bool(payload.get("force_refresh"))
    session = await _active_session(token, db)
    visitor_token = str(payload.get("visitor_token") or "").strip()
    lock_key = f"{token}:{visitor_token}" if visitor_token else f"{token}:{uuid4().hex}"
    async with _lock(_session_locks, lock_key):
        if not force_refresh and visitor_token:
            safe_prefix = visitor_token[:24].replace("%", "").replace("_", "")
            existing = (await db.execute(select(SharedScanWorker).where(SharedScanWorker.shared_session_id == token, SharedScanWorker.sub_session_id.like(f"{safe_prefix}:%")).order_by(SharedScanWorker.id.desc()).limit(1))).scalar_one_or_none()
            if existing and existing.status not in {"failed", "expired"}:
                return ok({"sub_session_id": existing.sub_session_id, "qrcode_data_url": existing.qr_code_url or ""}, "加入成功，请扫描二维码")
        result = await qr_login_manager.generate_qr_code()
        if not result.get("success"):
            raise HTTPException(502, result.get("message", "生成二维码失败"))
        prefix = visitor_token[:24].replace("%", "").replace("_", "") if visitor_token else uuid4().hex[:12]
        sub_session_id = f"{prefix}:{uuid4().hex}"
        worker = SharedScanWorker(
            shared_session_id=session.session_token,
            sub_session_id=sub_session_id,
            xianyu_session_id=str(result["session_id"]),
            status="qrcode_ready",
            qr_code_url=result.get("qr_code_url"),
        )
        db.add(worker)
        await db.commit()
        return ok({"sub_session_id": sub_session_id, "qrcode_data_url": worker.qr_code_url or ""}, "加入成功，请扫描二维码")


async def _finish_worker(worker: SharedScanWorker, session: SharedScanSession, db: AsyncSession) -> dict:
    cookies = qr_login_manager.cookies(worker.xianyu_session_id or "")
    if not cookies or not cookies.get("cookies"):
        worker.status = "failed"
        worker.error = "闲鱼已确认登录，但没有返回 Cookie"
        await db.commit()
        return _worker_data(worker)
    cookie_value = str(cookies["cookies"])
    unb = str(cookies.get("unb") or "").strip() or None
    nickname = str(cookies.get("nickname") or "").strip() or extract_account_nickname(cookie_value)
    login_expire_at = _now() + timedelta(days=30)
    account = None
    if unb:
        account = (await db.execute(select(Account).where(Account.user_id == session.owner_id, Account.goofish_id == unb))).scalar_one_or_none()
    if account is None:
        account = Account(
            user_id=session.owner_id,
            account_name=nickname or f"闲鱼账号-{unb or worker.sub_session_id[:8]}",
            goofish_id=unb,
            cookie=cookie_value,
            status="active",
            cookie_expire_at=login_expire_at,
        )
        db.add(account)
        await db.flush()
        is_new = True
    else:
        account.cookie = cookie_value
        account.status = "active"
        account.cookie_expire_at = login_expire_at
        if nickname and is_generated_account_name(account.account_name, account.goofish_id):
            account.account_name = nickname
        is_new = False
    db.add(AccountCookie(account_id=account.id, cookie_value=cookie_value, status="active", expires_at=login_expire_at))
    worker.status = "success"
    worker.cookie_saved = True
    worker.account_id = str(account.id)
    worker.error = None
    await db.commit()
    runtime = await _notify_runtime(account, cookie_value, session.owner_id, is_new)
    return {**_worker_data(worker), "runtime": runtime}


@router.get("/worker-status")
async def worker_status(sub_session_id: str = Query(...), db: AsyncSession = Depends(get_session)):
    worker = (await db.execute(select(SharedScanWorker).where(SharedScanWorker.sub_session_id == sub_session_id))).scalar_one_or_none()
    if worker is None:
        raise HTTPException(404, "兼职扫码会话不存在或已过期")
    if worker.cookie_saved or worker.status in {"success", "failed", "expired"}:
        return ok(_worker_data(worker), "扫码状态查询成功")
    async with _lock(_worker_locks, sub_session_id):
        await db.refresh(worker)
        if worker.cookie_saved:
            return ok(_worker_data(worker), "扫码登录成功")
        status = qr_login_manager.status(worker.xianyu_session_id or "")
        platform_status = status.get("status")
        if platform_status == "not_found":
            worker.status = "failed"
            worker.error = "二维码登录会话已不存在，请重新获取二维码"
            await db.commit()
            return ok(_worker_data(worker), worker.error)
        mapped = {"waiting": "qrcode_ready", "scanned": "scanning", "verification_required": "verification_required", "success": "success", "expired": "failed", "cancelled": "failed"}.get(platform_status, "qrcode_ready")
        worker.status = mapped
        worker.error = status.get("error")
        if mapped == "verification_required":
            await db.commit()
            return ok({**_worker_data(worker), "face_qr_url": status.get("face_qr_url"), "verification_url": status.get("verification_url")}, "需要完成手机人脸核验")
        if mapped == "success":
            session = (await db.execute(select(SharedScanSession).where(SharedScanSession.session_token == worker.shared_session_id))).scalar_one_or_none()
            if session is None:
                worker.status = "failed"; worker.error = "共享会话不存在"; await db.commit()
            else:
                data = await _finish_worker(worker, session, db)
                return ok(data, "扫码登录成功" if data.get("status") == "success" else data.get("message") or "扫码登录失败")
        await db.commit()
        return ok(_worker_data(worker), "扫码状态查询成功")


__all__ = ["router"]
