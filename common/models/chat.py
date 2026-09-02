"""实时闲鱼聊天消息。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from common.db.base import Base


class ChatMessageRecord(Base):
    """IM 推送解码后的消息快照，供聊天页历史读取和实时广播使用。"""

    __tablename__ = "xr_chat_message_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    cid: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(128), index=True)
    sender_id: Mapped[str | None] = mapped_column(String(128))
    sender_name: Mapped[str | None] = mapped_column(String(255))
    is_self: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    msg_type: Mapped[str] = mapped_column(String(16), default="text", nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    images: Mapped[list | None] = mapped_column(JSON)
    message_time: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True, nullable=False)
