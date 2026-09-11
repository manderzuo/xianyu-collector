# -*- coding: utf-8 -*-
"""闲鱼账号表:登录态 Cookie、状态、代理。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from common.db.base import Base


class Account(Base):
    """闲鱼账号及其登录态、代理与生命周期状态。

    ``status`` 只描述**登录态生命周期**（active / expired / paused / inactive）；
    IM 长连接的可用性单独记录在 ``im_status``，两者不能互相代替。
    早期实现把「取不到 IM Token」也写成 ``status = "expired"``，会让网页侧
    同步任务整个跳过该账号，把「聊天链路故障」放大成「整账号停摆」。
    """

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
    # IM 长连接状态：unknown / connected / expired / error。
    # 用于把「聊天链路不可用」与「登录态失效」区分开。
    im_status: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    # 最近一次续期**尝试**时间（不论成败），用于限制续期频率，
    # 避免多个定时任务在同一账号上反复打 Passport。
    last_renewal_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 最近一次续期**成功**时间，以及按当前调度约定的下次续期时间。
    # 这两个字段此前只存在于数据库、代码从不读写（死字段），
    # 会让排查时看到的时间与实际调度不符；现在由续期流程维护。
    cookie_last_renewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    cookie_next_renewal_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
