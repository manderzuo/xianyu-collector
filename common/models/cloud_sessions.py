# -*- coding: utf-8 -*-
"""云端加密闲鱼会话在本机的同步索引。"""
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base import Base


class CloudAccountSession(Base):
    __tablename__ = "xr_cloud_account_sessions"
    __table_args__ = (UniqueConstraint("owner_user_id", "account_key", name="uq_cloud_session_owner_account"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    account_key: Mapped[str] = mapped_column(String(128), nullable=False)
    account_name: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_metadata: Mapped[dict | None] = mapped_column("metadata", JSON)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
