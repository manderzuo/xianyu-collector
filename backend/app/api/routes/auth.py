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
from common.models import RegistrationInvite, User, SystemSetting, UserEntitlementOverride
from common.services.entitlements import ENTITLEMENT_FEATURES
from common.services.registration_invites import hash_invite_code, normalize_invite_code
from common.services.system_settings import registration_enabled
from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.core.security import create_access_token, create_refresh_token, decode_refresh_token, hash_password, verify_password
from backend.app.services.entitlements import entitlement_payload
from common.services.cloud_auth import CloudAuthError, cloud_auth_request, cloud_auth_url

router = APIRouter(prefix="/api/v1/auth", tags=["鉴权"])
refresh_bearer = HTTPBearer(auto_error=False)
DEFAULT_ADMIN_PASSWORD = "admin123"


def _cloud_role(value: object) -> str:
    return "admin" if str(value or "").strip().lower() == "admin" else "user"


def _cloud_expiry(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def normalize_cloud_overrides(value: object) -> dict[str, dict[str, object]]:
    """把云端 entitlements_json 规范化为可写入本机覆盖表的字段。

    只接受平台已支持的功能键，忽略无法解释的字段，避免云端返回的
    任意 JSON 污染本机授权表。
    """
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, object]] = {}
    for raw_key, raw_feature in value.items():
        key = str(raw_key or "").strip()
        if key not in ENTITLEMENT_FEATURES or not isinstance(raw_feature, dict):
            continue
        entry: dict[str, object] = {}
        if isinstance(raw_feature.get("enabled"), bool):
            entry["enabled"] = raw_feature["enabled"]
        if isinstance(raw_feature.get("unlimited"), bool):
            entry["unlimited"] = raw_feature["unlimited"]
        limit = raw_feature.get("limit", raw_feature.get("limit_value"))
        if limit is not None:
            try:
                parsed = int(limit)
            except (TypeError, ValueError):
                parsed = None
            if parsed is not None and parsed >= 0:
                entry["limit_value"] = parsed
        expires_at = _cloud_expiry(raw_feature.get("expires_at"))
        if expires_at is not None:
            entry["expires_at"] = expires_at
        reason = str(raw_feature.get("reason") or "").strip()[:255]
        if reason:
            entry["reason"] = reason
        result[key] = entry
    return result


async def _sync_cloud_overrides(session: AsyncSession, record: User, raw_overrides: object) -> None:
    """云端模式下把云端功能授权镜像到本机，使本机功能拦截与云端一致。

    云端统一认证是套餐与功能授权的权威来源，本机覆盖表只是执行镜像；
    不修改 auth_version，避免在登录时把用户已签发的令牌全部作废。

    注意：只有云端明确返回了 entitlements 字典时才做清理。旧版鉴权服务
    不返回该字段，此时必须保持本机授权不变，否则每次登录都会把用户授权清空。
    """
    if not isinstance(raw_overrides, dict):
        return
    overrides = normalize_cloud_overrides(raw_overrides)
    rows = list(
        (await session.execute(select(UserEntitlementOverride).where(UserEntitlementOverride.user_id == record.id))).scalars().all()
    )
    by_key = {str(row.feature_key): row for row in rows}
    for key, row in by_key.items():
        # 只清理平台受管功能键，保留其他历史数据。
        if key in ENTITLEMENT_FEATURES and key not in overrides:
            await session.delete(row)
    for key, values in overrides.items():
        row = by_key.get(key)
        if row is None:
            row = UserEntitlementOverride(user_id=record.id, feature_key=key)
            session.add(row)
        row.enabled = values.get("enabled") if isinstance(values.get("enabled"), bool) else None
        row.unlimited = values.get("unlimited") if isinstance(values.get("unlimited"), bool) else None
        row.limit_value = values.get("limit_value") if isinstance(values.get("limit_value"), int) else None
        row.expires_at = values.get("expires_at") if isinstance(values.get("expires_at"), datetime) else None
        row.reason = values.get("reason") if isinstance(values.get("reason"), str) else None


async def _authenticate_cloud_user(username: str, password: str) -> tuple[dict, str]:
    """Authenticate the system user and obtain the session used by /auth/verify.

    The local database remains a mirror for business ownership and quotas.  In
    cloud mode the password authority is the shared auth service, and the
    returned session token must travel inside the local JWT so every protected
    request can validate the same cloud session.
    """
    try:
        remote = await cloud_auth_request(
            "login",
            {"username": username, "password": password},
        )
    except CloudAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    remote_user = (remote or {}).get("user") or {}
    cloud_token = str((remote or {}).get("session_token") or "").strip()
    remote_username = str(remote_user.get("username") or "").strip()
    if not remote_username or not cloud_token:
        raise HTTPException(status_code=502, detail="云端登录响应缺少有效会话，请稍后重试")
    return remote_user, cloud_token


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
        if cloud_auth_url():
            raise HTTPException(status_code=409, detail="云端模式请使用用户名和密码登录")
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
    cloud_token = ""
    remote_user: dict = {}
    if cloud_auth_url():
        if not password:
            raise HTTPException(status_code=409, detail="云端模式请使用用户名和密码登录")
        # Cloud login is the password authority.  Email/password remains
        # compatible by resolving the local mirror to its cloud username.
        cloud_username = username or (str(user_record.username).strip() if user_record else "")
        if not cloud_username:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
        remote_user, cloud_token = await _authenticate_cloud_user(cloud_username, password)
        remote_username = str(remote_user["username"]).strip()
        if user_record is None or str(user_record.username).casefold() != remote_username.casefold():
            user_record = (
                await session.execute(select(User).where(User.username == remote_username).limit(1))
            ).scalar_one_or_none()
        if user_record is None:
            user_record = User(
                username=remote_username,
                password_hash=hash_password(password),
                nickname=str(remote_user.get("employee_name") or remote_username),
                role=_cloud_role(remote_user.get("role")),
                status=1,
                plan_code=str(remote_user.get("plan_code") or "NORMAL").upper(),
                plan_expires_at=_cloud_expiry(remote_user.get("plan_expires_at")),
            )
            session.add(user_record)
            await session.flush()
        else:
            # Keep the local mirror usable for ownership/quota lookups and for
            # a future explicitly configured local-auth fallback.  Do not sync
            # any Xianyu account Cookie/Token here.
            user_record.password_hash = hash_password(password)
            user_record.username = remote_username
            user_record.role = _cloud_role(remote_user.get("role"))
            user_record.status = 1
            user_record.plan_code = str(remote_user.get("plan_code") or user_record.plan_code or "NORMAL").upper()
            if "plan_expires_at" in remote_user:
                user_record.plan_expires_at = _cloud_expiry(remote_user.get("plan_expires_at"))
        # 云端功能授权是权威来源：镜像到本机，否则管理员在“套餐权限”里为
        # 单个用户开关的功能在本机拦截逻辑中不会生效。
        await _sync_cloud_overrides(session, user_record, remote_user.get("entitlements"))
        await session.commit()
        await session.refresh(user_record)
    elif user_record is None or not user_record.status or (password and not verify_password(password, user_record.password_hash)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    claims = {
        "sub": str(user_record.id),
        "username": user_record.username,
        "role": user_record.role,
        "plan_code": user_record.plan_code or "NORMAL",
        "auth_version": int(user_record.auth_version or 1),
    }
    if cloud_token:
        claims["cloud_session_token"] = cloud_token
    entitlements = await entitlement_payload(session, claims)
    user = {
        "id": user_record.id,
        "username": user_record.username,
        "nickname": user_record.nickname,
        "role": user_record.role,
        "plan_code": user_record.plan_code or "NORMAL",
        "plan_expires_at": user_record.plan_expires_at.isoformat() if user_record.plan_expires_at else None,
        "auth_version": int(user_record.auth_version or 1),
        "entitlements": entitlements,
        "account_limit": user_record.account_limit,
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
async def me(user=Depends(get_current_user)):
    return ok({"id": int(user.get("sub", 1)), "username": user.get("username", "admin"), "role": user.get("role", "admin")})


@router.post("/logout")
async def logout(user=Depends(get_current_user)):
    return ok({"logged_out": True})


@router.post("/register")
async def register(request: RegisterRequest, session: AsyncSession = Depends(get_session)):
    # 与 /system-settings/public 共用同一个读取函数，避免出现
    # “页面显示注册开放、提交却被拒绝”的分裂状态。
    if not await registration_enabled(session):
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
    # Cloud mode validates the shared session on every protected request.  A
    # refresh must carry that session forward or the next /auth/verify call
    # will immediately log the user out again.
    if claims.get("cloud_session_token"):
        base_claims["cloud_session_token"] = claims["cloud_session_token"]
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
async def verify(user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    role = str(user.get("role", "user")).lower()
    entitlements = await entitlement_payload(session, user)
    return ok({
        "valid": True,
        "authenticated": True,
        "user_id": int(user.get("sub", 1)),
        "username": user.get("username", ""),
        "is_admin": role in {"admin", "administrator"} or bool(user.get("is_admin")),
        "role": user.get("role", "user"),
        "plan_code": user.get("plan_code", "NORMAL"),
        "plan_expires_at": user.get("plan_expires_at"),
        "auth_version": user.get("auth_version", 1),
        "account_limit": user.get("account_limit"),
        "entitlements": entitlements,
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
