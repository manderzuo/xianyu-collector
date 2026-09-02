# -*- coding: utf-8 -*-
"""消息表:会话消息记录,自动回复路由的依据。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from common.db.base import Base


class Message(Base):
    """会话消息流水。"""

    __tablename__ = "xr_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    peer_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # in / out
    content: Mapped[str | None] = mapped_column(Text)
    msg_type: Mapped[str] = mapped_column(String(16), default="text", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True, nullable=False
    )
