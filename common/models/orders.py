# -*- coding: utf-8 -*-
"""订单表:拉单、自动评价、超时检测的数据源。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, JSON, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from common.db.base import Base


class Order(Base):
    """订单及其支付、发货和售后状态。"""

    __tablename__ = "xr_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    order_no: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    buyer_id: Mapped[str | None] = mapped_column(String(64))
    buyer_nick: Mapped[str | None] = mapped_column(String(255))
    product_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    item_external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    item_title: Mapped[str | None] = mapped_column(String(255))
    quantity: Mapped[int] = mapped_column(default=1, nullable=False)
    spec_name: Mapped[str | None] = mapped_column(String(128))
    spec_value: Mapped[str | None] = mapped_column(String(255))
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    is_rated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_red_flower: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    placed_at: Mapped[datetime | None] = mapped_column(DateTime)
    payload: Mapped[dict | None] = mapped_column(JSON)
    delivery_method: Mapped[str | None] = mapped_column(String(16))
    delivery_content: Mapped[str | None] = mapped_column(Text)
    delivery_fail_reason: Mapped[str | None] = mapped_column(Text)
    delivery_send_status: Mapped[str | None] = mapped_column(String(16))
    delivery_send_fail_reason: Mapped[str | None] = mapped_column(Text)
    card_only_delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
