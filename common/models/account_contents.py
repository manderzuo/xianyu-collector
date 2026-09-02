# -*- coding: utf-8 -*-
"""账号从闲鱼同步回来的内容快照。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from common.db.base import Base


class AccountContent(Base):
    """账号级平台内容。

    目前同步器落库商品，``content_type`` 为 product；预留同一张表承接
    订单、会话等账号内容，平台原始字段统一保存在 ``payload`` 中。
    """

    __tablename__ = "xr_account_contents"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "content_type",
            "external_id",
            name="uq_account_content_external",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    content_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    stock: Mapped[int] = mapped_column(default=0, nullable=False)
    images: Mapped[list | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="on_sale", nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class AccountSyncState(Base):
    """账号内容同步的最后一次状态和各类统计。"""

    __tablename__ = "xr_account_sync_states"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="never", nullable=False)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    products_count: Mapped[int] = mapped_column(default=0, nullable=False)
    orders_count: Mapped[int] = mapped_column(default=0, nullable=False)
    messages_count: Mapped[int] = mapped_column(default=0, nullable=False)
    last_result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
