# -*- coding: utf-8 -*-
"""FastAPI 依赖项。"""
from __future__ import annotations

from typing import Any
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from common.config import settings
from common.db.session import get_session
from common.models import User, UserEntitlementOverride
from backend.app.core.security import decode_access_token
from common.services.cloud_auth import CloudAuthError, cloud_auth_request, cloud_auth_url

bearer = HTTPBearer(auto_error=False)
DEVELOPMENT_USER = {"sub": "1", "username": "admin", "role": "admin"}


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session=Depends(get_session),
) -> dict[str, Any]:
    if credentials:
        user = decode_access_token(credentials.credentials)
        if user:
            try:
                user_id = int(user.get("sub", 0))
            except (TypeError, ValueError):
                user_id = 0
            if user_id <= 0:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已失效")
            try:
                record = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
            except SQLAlchemyError as exc:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="用户状态暂不可用") from exc
            if record is None:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已被停用")
            if int(record.status or 0) == 2:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号正在审核中，请等待管理员审批")
            if int(record.status or 0) != 1:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已被停用")
            token_version = int(user.get("auth_version", 1) or 1)
            if int(record.auth_version or 1) != token_version:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已失效，请重新登录")
            if cloud_auth_url():
                cloud_token = str(user.get("cloud_session_token") or "").strip()
                if not cloud_token:
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="云端登录状态已失效，请重新登录")
                try:
                    remote = await cloud_auth_request("me", {}, cloud_token)
                except CloudAuthError as exc:
                    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
                remote_user = (remote or {}).get("user") or {}
                if remote_user.get("username") and str(remote_user["username"]).casefold() != str(record.username).casefold():
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="云端登录身份不匹配，请重新登录")
                remote_role = str(remote_user.get("role") or "user").lower()
                record.role = "admin" if remote_role == "admin" else "user"
                record.status = 1
                # Mirror shared plan/feature overrides into this computer's
                # business database so quota checks use the same authority.
                if remote_user.get("plan_code"):
                    record.plan_code = str(remote_user.get("plan_code") or "NORMAL").upper()
                if "plan_expires_at" in remote_user:
                    from datetime import datetime
                    try:
                        record.plan_expires_at = datetime.fromisoformat(str(remote_user.get("plan_expires_at")).replace("Z", "+00:00")).replace(tzinfo=None) if remote_user.get("plan_expires_at") else None
                    except ValueError:
                        pass
                remote_overrides = remote_user.get("entitlements") or {}
                if isinstance(remote_overrides, dict):
                    current = list((await session.execute(select(UserEntitlementOverride).where(UserEntitlementOverride.user_id == record.id))).scalars().all())
                    by_key = {item.feature_key: item for item in current}
                    for stale in current:
                        if stale.feature_key not in remote_overrides:
                            await session.delete(stale)
                    for feature_key, values in remote_overrides.items():
                        if not isinstance(values, dict):
                            continue
                        item = by_key.get(feature_key) or UserEntitlementOverride(user_id=record.id, feature_key=str(feature_key))
                        if item not in current:
                            session.add(item)
                        for attr in ("enabled", "unlimited", "reason"):
                            if attr in values:
                                setattr(item, attr, values[attr])
                        if "limit" in values:
                            item.limit_value = values.get("limit")
                        if "expires_at" in values:
                            try:
                                item.expires_at = datetime.fromisoformat(str(values["expires_at"]).replace("Z", "+00:00")).replace(tzinfo=None) if values["expires_at"] else None
                            except ValueError:
                                pass
                await session.commit()
            expires_at = record.plan_expires_at or record.expire_at
            return {
                **user,
                "id": record.id,
                "sub": str(record.id),
                "username": record.username,
                "nickname": record.nickname,
                "role": record.role,
                "is_admin": str(record.role or "").lower() in {"admin", "administrator"},
                "plan_code": record.plan_code or "NORMAL",
                "plan_expires_at": expires_at.isoformat() if expires_at else None,
                "account_limit": record.account_limit,
                "auth_version": int(record.auth_version or 1),
            }
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已失效")
    if settings.environment != "production" and settings.allow_dev_auth_bypass:
        return DEVELOPMENT_USER
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
