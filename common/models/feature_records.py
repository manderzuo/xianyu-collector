"""旧版多个配置型模块共用的结构化记录表。

这些模块的字段会随业务版本扩展，核心字段统一落库，扩展字段保存在 JSON
中，避免为了兼容旧版每个小模块都退化成内存演示数据。
"""
from datetime import datetime
from sqlalchemy import JSON, BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base import Base


class FeatureRecord(Base):
    __tablename__ = "xr_feature_records"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    feature: Mapped[str] = mapped_column(String(96), index=True, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
