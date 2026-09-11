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
    # 取 IM Token 与建立长连接时使用的设备指纹（IM 侧的 did）。
    # 必须跨请求、跨重连保持稳定：每次随机生成新设备指纹会被平台判定为
    # 不可信环境，进而拒绝下发长登录凭据。首次使用时生成并落库，此后复用。
    im_device_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
