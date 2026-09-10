# -*- coding: utf-8 -*-
"""系统用户注册邀请码。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from common.db.base import Base


class RegistrationInvite(Base):
    """管理员生成的一次性注册邀请码。

    原始邀请码不落库，数据库只保存 SHA-256 摘要和脱敏预览值。
    完整邀请码仅在生成接口的响应中返回一次。
    """

    __tablename__ = "xr_registration_invites"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    code_preview: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    used_by: Mapped[int | None] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
