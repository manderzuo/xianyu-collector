# -*- coding: utf-8 -*-
"""套餐默认值和授权初始化。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.entitlements import Plan, PlanEntitlement
from common.models.users import User


FEATURE_ACCOUNT = "account.manage"
FEATURE_CARD_AUTO_DELIVERY = "card.auto_delivery"
FEATURE_PRODUCT_AUTO_PUBLISH = "product.auto_publish"
FEATURE_AI_SMART_REPLY = "ai.smart_reply"
FEATURE_BUILTIN_AI_REPLY = "ai.builtin_reply"
FEATURE_KEYWORD_REPLY = "keyword.reply"

ENTITLEMENT_FEATURES = (
    FEATURE_ACCOUNT,
    FEATURE_CARD_AUTO_DELIVERY,
    FEATURE_PRODUCT_AUTO_PUBLISH,
    FEATURE_AI_SMART_REPLY,
    FEATURE_BUILTIN_AI_REPLY,
    FEATURE_KEYWORD_REPLY,
)

DEFAULT_PLAN_ENTITLEMENTS = {
    "NORMAL": {
        FEATURE_ACCOUNT: {"enabled": True, "limit_value": 1, "unlimited": False},
        FEATURE_CARD_AUTO_DELIVERY: {"enabled": True, "limit_value": 1, "unlimited": False},
        FEATURE_PRODUCT_AUTO_PUBLISH: {"enabled": True, "limit_value": 1, "unlimited": False},
        FEATURE_AI_SMART_REPLY: {"enabled": False, "limit_value": None, "unlimited": False},
        FEATURE_BUILTIN_AI_REPLY: {"enabled": False, "limit_value": None, "unlimited": False},
        FEATURE_KEYWORD_REPLY: {"enabled": True, "limit_value": 5, "unlimited": False},
    },
    "VIP": {
        FEATURE_ACCOUNT: {"enabled": True, "limit_value": None, "unlimited": True},
        FEATURE_CARD_AUTO_DELIVERY: {"enabled": True, "limit_value": None, "unlimited": True},
        FEATURE_PRODUCT_AUTO_PUBLISH: {"enabled": True, "limit_value": None, "unlimited": True},
        FEATURE_AI_SMART_REPLY: {"enabled": True, "limit_value": None, "unlimited": True},
        FEATURE_BUILTIN_AI_REPLY: {"enabled": True, "limit_value": None, "unlimited": True},
        FEATURE_KEYWORD_REPLY: {"enabled": True, "limit_value": None, "unlimited": True},
    },
}


async def ensure_default_plans(session: AsyncSession) -> None:
    """创建缺失的默认套餐和授权，不覆盖管理员已经调整的值。"""
    for code, values in DEFAULT_PLAN_ENTITLEMENTS.items():
        plan = (await session.execute(select(Plan).where(Plan.code == code))).scalar_one_or_none()
        if plan is None:
            plan = Plan(code=code, name="普通用户" if code == "NORMAL" else "VIP 用户", is_default=code == "NORMAL")
            session.add(plan)
            await session.flush()
        existing = {
            row.feature_key: row
            for row in (await session.execute(select(PlanEntitlement).where(PlanEntitlement.plan_id == plan.id))).scalars().all()
        }
        for feature_key, config in values.items():
            if feature_key in existing:
                continue
            session.add(PlanEntitlement(plan_id=plan.id, feature_key=feature_key, **config))

    await session.execute(
        User.__table__.update().where(User.plan_code.is_(None)).values(plan_code="NORMAL")
    )
    await session.commit()
