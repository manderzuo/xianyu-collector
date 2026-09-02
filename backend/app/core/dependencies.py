# -*- coding: utf-8 -*-
"""FastAPI 依赖项。"""
from __future__ import annotations

from typing import Any
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from common.config import settings
from backend.app.core.security import decode_access_token

bearer = HTTPBearer(auto_error=False)
DEVELOPMENT_USER = {"sub": "1", "username": "admin", "role": "admin"}


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict[str, Any]:
    if credentials:
        user = decode_access_token(credentials.credentials)
        if user:
            return user
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已失效")
    if settings.environment != "production":
        return DEVELOPMENT_USER
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
