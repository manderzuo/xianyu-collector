# -*- coding: utf-8 -*-
"""闲鱼账号表:登录态 Cookie、状态、代理。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from common.db.base import Base


class Account(Base):
    """闲鱼账号及其登录态、代理与生命周期状态。"""

    __tablename__ = "xr_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    account_name: Mapped[str] = mapped_column(String(64), nullable=False)
    goofish_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    cookie: Mapped[str | None] = mapped_column(Text)
    proxy: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default="inactive", nullable=False)
    cookie_expire_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
