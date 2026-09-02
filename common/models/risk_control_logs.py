"""滑块及其他风控处理日志。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from common.db.base import Base


class RiskControlLog(Base):
    __tablename__ = "xr_risk_control_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    account_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    event_type: Mapped[str] = mapped_column(String(64), default="risk", nullable=False)
    event_description: Mapped[str | None] = mapped_column(Text)
    processing_result: Mapped[str | None] = mapped_column(Text)
    processing_status: Mapped[str] = mapped_column(String(20), default="processing", index=True, nullable=False)
    captcha_engine: Mapped[str | None] = mapped_column(String(32))
    call_type: Mapped[str | None] = mapped_column(String(16), index=True)
    call_user: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
