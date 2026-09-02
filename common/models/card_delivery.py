# -*- coding: utf-8 -*-
"""卡券发货流水与幂等记录。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, JSON, String, Text, func, Index
from sqlalchemy.orm import Mapped, mapped_column

from common.db.base import Base


class CardDeliveryRecord(Base):
    """一笔订单的一次卡券发货尝试。

    ``sending`` 用于防止重复点击和并发重复耗卡；只有真正收到闲鱼发送
    请求成功结果后才会变成 ``sent``。失败记录保留，方便重试和定位问题。
    """

    __tablename__ = "xr_card_delivery_records"
    __table_args__ = (
        Index("ix_card_delivery_owner_order", "owner_id", "order_id"),
        Index("ix_card_delivery_order_status", "order_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    order_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    order_no: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    card_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    card_name: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(16), default="own", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="sending", nullable=False)
    delivery_content: Mapped[str | None] = mapped_column(Text)
    message_ids: Mapped[list | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(default=1, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
