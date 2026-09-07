# -*- coding: utf-8 -*-
from datetime import datetime
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from common.schemas.api import LoginRequest
from common.db.session import get_session
from common.models import RegistrationInvite, User, SystemSetting
from common.services.registration_invites import hash_invite_code, normalize_invite_code
from backend.app.services.entitlements import entitlement_payload
from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.core.security import create_access_token, create_refresh_token, decode_refresh_token, hash_password, verify_password
from common.services.cloud_auth import CloudAuthError, cloud_auth_request, cloud_auth_url

router = APIRouter(prefix="/api/v1/auth", tags=["鉴权"])
refresh_bearer = HTTPBearer(auto_error=False)


def get_bootstrap_admin_password() -> str:
    return os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "").strip()


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    invite_code: str = Field(min_length=8, max_length=128)
    nickname: str | None = None
    session_id: str | None = Field(default=None, max_length=128)
    # 保留可选邮箱字段，供旧客户端写入用户资料；注册不再校验邮箱验证码。
    email: str | None = None


class PasswordRequest(BaseModel):
    old_password: str | None = None
    new_password: str = Field(min_length=6, max_length=128)


class ResetPasswordRequest(BaseModel):
    email: str = Field(min_length=3, max_length=128)
    verification_code: str = Field(min_length=4, max_length=8)
    new_password: str = Field(min_length=6, max_length=128)


async def ensure_admin(session: AsyncSession) -> User:
    result = await session.execute(select(User).where(User.username == "admin"))
    user = result.scalar_one_or_none()
    if user is not None:
        return user
    initial_password = get_bootstrap_admin_password() or secrets.token_urlsafe(32)
    user = User(username="admin", nickname="系统管理员", role="admin", status=1, plan_code="NORMAL", password_hash=hash_password(initial_password))
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@router.post("/login")
async def login(request: LoginRequest, session: AsyncSession = Depends(get_session)):
    try:
        user_record = await ensure_admin(session)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="数据库暂不可用，请稍后重试") from exc
    username = (request.username or "").strip()
    email = (request.email or "").strip().lower()
    password = request.password or ""
    remote = None

    # 云端模式统一校验账号状态，保证管理员在任意电脑审批后立即生效。
    # 本机仍建立一个同名镜像用户，用于归属该电脑上的闲鱼业务数据。
    if password and username:
        try:
            remote = await cloud_auth_request("login", {"username": username, "password": password})
        except CloudAuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        if remote is not None:
            remote_user = remote.get("user") or {}
            user_record = (await session.execute(select(User).where(User.username == username).limit(1))).scalar_one_or_none()
            if user_record is None:
                user_record = User(username=username, password_hash=hash_password(secrets.token_urlsafe(32)), role="user", status=1)
                session.add(user_record)
            user_record.nickname = remote_user.get("employee_name") or user_record.nickname
            user_record.role = "admin" if remote_user.get("role") == "admin" else "user"
            user_record.status = 1
            await session.commit()
            await session.refresh(user_record)

    # 密码登录开启滑块时必须使用已经由 geetest 路由确认过的一次性 challenge。
    if password and request.geetest_challenge:
        captcha_enabled = (
            await session.execute(
                select(SystemSetting.setting_value)
                .where(SystemSetting.setting_key == "login_captcha_enabled")
                .limit(1)
            )
        ).scalar_one_or_none()
        if str(captcha_enabled or "").lower() in {"1", "true", "yes", "on"}:
            from backend.app.api.routes.geetest import check_geetest_verified
            verified, message = check_geetest_verified(request.geetest_challenge)
            if not verified:
                raise HTTPException(status_code=401, detail=message)
    if email and request.verification_code and not password:
        from backend.app.api.routes.captcha import check_email_code
        verified, message = check_email_code(email, request.verification_code, "login")
        if not verified:
            raise HTTPException(status_code=401, detail=message)
        result = await session.execute(select(User).where(User.email == email).limit(1))
    elif password and username:
        result = await session.execute(select(User).where(User.username == username).limit(1))
    elif password and email:
        result = await session.execute(select(User).where(User.email == email).limit(1))
    else:
        raise HTTPException(status_code=422, detail="请提供用户名或邮箱及登录凭据")
    if user_record is None:
        user_record = result.scalar_one_or_none()
    if user_record is None or (password and not verify_password(password, user_record.password_hash) and not cloud_auth_url()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    if int(user_record.status or 0) == 2:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号正在审核中，请等待管理员审批")
    if int(user_record.status or 0) != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户已被停用")
    user = {
        "id": user_record.id,
        "username": user_record.username,
        "nickname": user_record.nickname,
        "role": user_record.role,
        "is_admin": str(user_record.role or "").lower() in {"admin", "administrator"},
        "plan_code": user_record.plan_code or "NORMAL",
        "account_limit": user_record.account_limit,
        "auth_version": int(user_record.auth_version or 1),
    }
    if remote is not None:
        user["cloud_session_token"] = remote.get("session_token")
    user["entitlements"] = await entitlement_payload(session, {"sub": str(user_record.id), **user})
    claims = {
        "sub": str(user["id"]),
        "username": user["username"],
        "role": user["role"],
        "auth_version": user["auth_version"],
        "cloud_session_token": user.get("cloud_session_token", ""),
    }
    token, expires_in = create_access_token(claims)
    refresh_token, refresh_expires_in = create_refresh_token(claims)
    return ok({
        "access_token": token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": expires_in,
        "refresh_expires_in": refresh_expires_in,
        "user": user,
    })


@router.get("/me")
async def me(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return ok({
        "id": int(user.get("sub", 1)),
        "username": user.get("username", "admin"),
        "nickname": user.get("nickname"),
        "role": user.get("role", "admin"),
        "is_admin": bool(user.get("is_admin")),
        "plan_code": user.get("plan_code", "NORMAL"),
        "account_limit": user.get("account_limit"),
        "auth_version": int(user.get("auth_version") or 1),
        "entitlements": await entitlement_payload(db, user),
    })


@router.post("/logout")
async def logout(user=Depends(get_current_user)):
    return ok({"logged_out": True})


@router.post("/register")
async def register(request: RegisterRequest, session: AsyncSession = Depends(get_session)):
    registration_enabled = (
        await session.execute(
            select(SystemSetting.setting_value)
            .where(SystemSetting.setting_key == "registration_enabled")
            .limit(1)
        )
    ).scalar_one_or_none()
    if registration_enabled is not None and str(registration_enabled).strip().lower() not in {"1", "true", "yes", "on"}:
        raise HTTPException(status_code=403, detail="注册功能已关闭，请联系管理员")
    invite_code = normalize_invite_code(request.invite_code)
    if len(invite_code) < 8:
        raise HTTPException(status_code=400, detail="邀请码格式无效")
    email = (request.email or "").strip().lower() or None

    # In cloud mode the invite belongs to the shared auth service.  Do not
    # look it up in this computer's business database, otherwise a code issued
    # on the administrator's computer can never work on another computer.
    if cloud_auth_url():
        try:
            remote = await cloud_auth_request("register", {
                "username": request.username.strip(),
                "password": request.password,
                "nickname": request.nickname or request.username.strip(),
                "invite_code": invite_code,
            })
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if remote is not None:
            return ok(remote.get("user") or {}, remote.get("message") or "注册申请已提交，请等待管理员审核")

    existing = (await session.execute(select(User).where(User.username == request.username.strip()))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")

    # 锁定邀请码记录后再核销，保证同一个邀请码在并发注册时只能成功一次。
    invite = (
        await session.execute(
            select(RegistrationInvite)
            .where(RegistrationInvite.code_hash == hash_invite_code(invite_code))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if invite is None:
        raise HTTPException(status_code=400, detail="邀请码无效，请向管理员索取有效邀请码")
    now = datetime.now()
    if invite.status != "active":
        status_message = {
            "used": "邀请码已使用",
            "revoked": "邀请码已撤销",
            "expired": "邀请码已过期",
        }.get(invite.status, "邀请码不可用")
        raise HTTPException(status_code=400, detail=status_message)
    if invite.expires_at and invite.expires_at <= now:
        invite.status = "expired"
        await session.commit()
        raise HTTPException(status_code=400, detail="邀请码已过期")

    user = User(username=request.username.strip(), password_hash=hash_password(request.password), nickname=request.nickname, email=email, role="user", plan_code="NORMAL", status=2)
    session.add(user)
    invite.status = "used"
    invite.used_at = now
    try:
        await session.flush()
        invite.used_by = user.id
        await session.commit(); await session.refresh(user)
    except SQLAlchemyError as exc:
        await session.rollback(); raise HTTPException(status_code=409, detail="注册失败，用户名可能已存在") from exc
    return ok({"id": user.id, "username": user.username, "role": user.role, "status": "PENDING"}, "注册申请已提交，请等待管理员审核")


@router.post("/refresh")
async def refresh(credentials: HTTPAuthorizationCredentials | None = Depends(refresh_bearer), session: AsyncSession = Depends(get_session)):
    if credentials is None:
        raise HTTPException(status_code=401, detail="缺少刷新令牌")
    claims = decode_refresh_token(credentials.credentials)
    # 兼容升级前把 access token 写入 refresh_token 的旧浏览器会话；
    # 只在旧令牌仍未过期时接受一次，并立即换发正确的刷新令牌。
    if claims is None:
        from backend.app.core.security import decode_access_token
        claims = decode_access_token(credentials.credentials)
    if claims is None:
        raise HTTPException(status_code=401, detail="刷新令牌已失效")
    # 云端模式的 access/refresh token 必须携带云端会话。旧版本在切换
    # 到云端认证前签发的本机令牌不能继续使用，否则管理员接口会落回
    # 本机用户表，造成不同电脑看到的注册申请不一致。
    if cloud_auth_url() and not str(claims.get("cloud_session_token") or "").strip():
        raise HTTPException(status_code=401, detail="云端登录状态已失效，请重新登录")
    try:
        user_id = int(claims.get("sub", 0))
    except (TypeError, ValueError):
        user_id = 0
    record = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if record is None or int(record.status or 0) != 1:
        if record is not None and int(record.status or 0) == 2:
            raise HTTPException(status_code=403, detail="账号正在审核中，请等待管理员审批")
        raise HTTPException(status_code=401, detail="用户不存在或已被停用")
    if int(record.auth_version or 1) != int(claims.get("auth_version", 1) or 1):
        raise HTTPException(status_code=401, detail="刷新令牌已失效，请重新登录")
    base_claims = {
        "sub": str(record.id),
        "username": record.username,
        "role": record.role,
        "auth_version": int(record.auth_version or 1),
        "cloud_session_token": claims.get("cloud_session_token", ""),
    }
    token, expires_in = create_access_token(base_claims)
    refresh_token, refresh_expires_in = create_refresh_token(base_claims)
    return ok({
        "access_token": token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": expires_in,
        "refresh_expires_in": refresh_expires_in,
    }, "令牌已刷新")


@router.get("/verify")
async def verify(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    role = str(user.get("role", "user")).lower()
    data = {
        "valid": True,
        "authenticated": True,
        "user_id": int(user.get("sub", 1)),
        "username": user.get("username", ""),
        "is_admin": role in {"admin", "administrator"} or bool(user.get("is_admin")),
        "role": user.get("role", "user"),
        "plan_code": user.get("plan_code", "NORMAL"),
        "account_limit": user.get("account_limit"),
        "auth_version": int(user.get("auth_version") or 1),
    }
    data["entitlements"] = await entitlement_payload(db, user)
    return ok(data, "登录状态有效")


@router.get("/check-default-password")
async def check_default_password(user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    record = (await session.execute(select(User).where(User.id == int(user.get("sub", 1))))).scalar_one_or_none()
    configured_password = get_bootstrap_admin_password()
    return ok({"is_default": bool(configured_password and record and verify_password(configured_password, record.password_hash))}, "查询成功")


@router.post("/change-password")
async def change_password(payload: PasswordRequest, user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    record = (await session.execute(select(User).where(User.id == int(user.get("sub", 1))))).scalar_one_or_none()
    if record is None: raise HTTPException(404, "用户不存在")
    if payload.old_password is not None and not verify_password(payload.old_password, record.password_hash): raise HTTPException(401, "原密码不正确")
    record.password_hash = hash_password(payload.new_password)
    record.auth_version = int(record.auth_version or 1) + 1
    await session.commit()
    return ok({"changed": True}, "密码已更新")


@router.post("/reset-password")
async def reset_password(payload: ResetPasswordRequest, session: AsyncSession = Depends(get_session)):
    email = payload.email.strip().lower()
    from backend.app.api.routes.captcha import check_email_code
    verified, message = check_email_code(email, payload.verification_code, "reset_password")
    if not verified:
        return {"success": False, "code": "email_code_invalid", "message": message, "data": None}
    record = (await session.execute(select(User).where(User.email == email).limit(1))).scalar_one_or_none()
    if record is None:
        return {"success": False, "code": "email_not_found", "message": "该邮箱未注册", "data": None}
    record.password_hash = hash_password(payload.new_password)
    record.auth_version = int(record.auth_version or 1) + 1
    await session.commit()
    return ok({"changed": True}, "密码重置成功")
