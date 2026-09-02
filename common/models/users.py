# -*- coding: utf-8 -*-
"""用户表:后台系统登录账号。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from common.db.base import Base


class User(Base):
    """系统用户与角色权限。"""

    __tablename__ = "xr_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    status: Mapped[int] = mapped_column(default=1, nullable=False)  # 1 正常 / 0 禁用
    account_limit: Mapped[int | None] = mapped_column(default=None)
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0, nullable=False)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
