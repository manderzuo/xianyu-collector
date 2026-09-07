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
from common.models import User
from backend.app.core.security import decode_access_token

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
