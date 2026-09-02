# -*- coding: utf-8 -*-
"""轻量级公共 Pydantic 契约。"""
from typing import Any, Generic, TypeVar
from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    success: bool = True
    code: str = "ok"
    message: str = "操作成功"
    data: T | None = None


class LoginRequest(BaseModel):
    """兼容用户名、邮箱密码和邮箱验证码三种登录方式。"""

    username: str | None = Field(default=None, min_length=1, max_length=64)
    email: str | None = Field(default=None, min_length=3, max_length=128)
    password: str | None = Field(default=None, min_length=1, max_length=128)
    verification_code: str | None = Field(default=None, min_length=4, max_length=8)
    geetest_challenge: str | None = Field(default=None, max_length=256)


class TokenData(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict[str, Any]
