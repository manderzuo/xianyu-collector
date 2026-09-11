# -*- coding: utf-8 -*-
"""系统设置读取的共享逻辑。

登录前的公开设置接口和真正的业务校验接口必须使用**同一套默认值**，
否则会出现“页面显示注册开放、用户填完表单却被拒绝”的分裂状态
（`/system-settings/public` 曾经在设置行缺失时返回 true，而
`/auth/register` 在设置行缺失时按关闭处理）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models import SystemSetting

#: 前端只识别 true / 'true' / 1 / '1'，这里额外兼容 yes / on，
#: 并由 :func:`registration_enabled` 统一归一化为规范字符串。
TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
FALSY_VALUES = frozenset({"0", "false", "no", "off"})

REGISTRATION_ENABLED_KEY = "registration_enabled"

#: 注册默认开启：注册本身仍受管理员发放的邀请码约束，
#: 邀请码才是真正的准入控制。默认关闭会让未配置过系统设置的
#: 全新部署无法注册，且与公开设置接口和设置页的既有默认值矛盾。
DEFAULT_REGISTRATION_ENABLED = True


def parse_bool_value(value: Any, default: bool = False) -> bool:
    """把系统设置里的字符串/布尔值归一化为布尔。

    无法识别（含空值与 None）时返回 ``default``。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in TRUTHY_VALUES:
        return True
    if text in FALSY_VALUES:
        return False
    return default


async def read_setting(session: AsyncSession, key: str) -> str | None:
    """读取单个系统设置项；不存在时返回 None（不创建行）。"""
    return (
        await session.execute(
            select(SystemSetting.setting_value).where(SystemSetting.setting_key == key).limit(1)
        )
    ).scalar_one_or_none()


async def registration_enabled(session: AsyncSession) -> bool:
    """邀请码注册是否开放。

    公开设置接口与注册接口都必须调用本函数，保证两者永远一致。
    """
    return parse_bool_value(await read_setting(session, REGISTRATION_ENABLED_KEY), DEFAULT_REGISTRATION_ENABLED)
