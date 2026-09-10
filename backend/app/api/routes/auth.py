# -*- coding: utf-8 -*-
from datetime import datetime

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
from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.core.security import create_access_token, create_refresh_token, decode_refresh_token, hash_password, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["鉴权"])
refresh_bearer = HTTPBearer(auto_error=False)
DEFAULT_ADMIN_PASSWORD = "admin123"


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
    user = User(username="admin", nickname="系统管理员", role="admin", status=1, password_hash=hash_password(DEFAULT_ADMIN_PASSWORD))
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
    user_record = result.scalar_one_or_none()
    if user_record is None or not user_record.status or (password and not verify_password(password, user_record.password_hash)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    user = {
        "id": user_record.id,
        "username": user_record.username,
        "nickname": user_record.nickname,
        "role": user_record.role,
    }
    claims = {"sub": str(user["id"]), "username": user["username"], "role": user["role"]}
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
async def me(user=Depends(get_current_user)):
    return ok({"id": int(user.get("sub", 1)), "username": user.get("username", "admin"), "role": user.get("role", "admin")})


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
    if str(registration_enabled or "").strip().lower() not in {"1", "true", "yes", "on"}:
        raise HTTPException(status_code=403, detail="注册功能已关闭，请联系管理员")
    invite_code = normalize_invite_code(request.invite_code)
    if len(invite_code) < 8:
        raise HTTPException(status_code=400, detail="邀请码格式无效")
    email = (request.email or "").strip().lower() or None
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

    if not request.session_id:
        raise HTTPException(status_code=400, detail="请先完成图形验证码")
    from backend.app.api.routes.captcha import consume_captcha
    captcha_verified, captcha_message = consume_captcha(request.session_id)
    if not captcha_verified:
        raise HTTPException(status_code=400, detail=captcha_message)

    user = User(username=request.username.strip(), password_hash=hash_password(request.password), nickname=request.nickname, email=email, role="user", status=1)
    session.add(user)
    invite.status = "used"
    invite.used_at = now
    try:
        await session.flush()
        invite.used_by = user.id
        await session.commit(); await session.refresh(user)
    except SQLAlchemyError as exc:
        await session.rollback(); raise HTTPException(status_code=409, detail="注册失败，用户名可能已存在") from exc
    return ok({"id": user.id, "username": user.username, "role": user.role}, "注册成功")


@router.post("/refresh")
async def refresh(credentials: HTTPAuthorizationCredentials | None = Depends(refresh_bearer)):
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
    base_claims = {
        "sub": str(claims.get("sub", "1")),
        "username": claims.get("username", ""),
        "role": claims.get("role", "user"),
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
async def verify(user=Depends(get_current_user)):
    role = str(user.get("role", "user")).lower()
    return ok({
        "valid": True,
        "authenticated": True,
        "user_id": int(user.get("sub", 1)),
        "username": user.get("username", ""),
        "is_admin": role in {"admin", "administrator"} or bool(user.get("is_admin")),
    }, "登录状态有效")


@router.get("/check-default-password")
async def check_default_password(user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    record = (await session.execute(select(User).where(User.id == int(user.get("sub", 1))))).scalar_one_or_none()
    return ok({"is_default": bool(record and verify_password(DEFAULT_ADMIN_PASSWORD, record.password_hash))}, "查询成功")


@router.post("/change-password")
async def change_password(payload: PasswordRequest, user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    record = (await session.execute(select(User).where(User.id == int(user.get("sub", 1))))).scalar_one_or_none()
    if record is None: raise HTTPException(404, "用户不存在")
    if payload.old_password is not None and not verify_password(payload.old_password, record.password_hash): raise HTTPException(401, "原密码不正确")
    record.password_hash = hash_password(payload.new_password); await session.commit()
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
    await session.commit()
    return ok({"changed": True}, "密码重置成功")
