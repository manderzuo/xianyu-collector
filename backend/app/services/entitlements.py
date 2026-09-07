# -*- coding: utf-8 -*-
"""统一的角色、套餐、功能授权和配额服务。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models import Account, FeatureRecord, Plan, PlanEntitlement, QuotaReservation, User, UserEntitlementOverride
from common.services.entitlements import (
    DEFAULT_PLAN_ENTITLEMENTS,
    ENTITLEMENT_FEATURES,
    FEATURE_ACCOUNT,
    FEATURE_AI_SMART_REPLY,
    FEATURE_CARD_AUTO_DELIVERY,
    FEATURE_KEYWORD_REPLY,
    FEATURE_PRODUCT_AUTO_PUBLISH,
)


@dataclass(frozen=True)
class EffectiveEntitlement:
    feature_key: str
    enabled: bool
    limit_value: int | None
    unlimited: bool
    plan_code: str
    source: str
    expires_at: datetime | None = None

    def as_dict(self, used: int = 0, reserved: int = 0) -> dict[str, Any]:
        remaining = None if self.unlimited else max((self.limit_value or 0) - used - reserved, 0)
        return {
            "feature_key": self.feature_key,
            "enabled": self.enabled,
            "limit": None if self.unlimited else self.limit_value,
            "unlimited": self.unlimited,
            "used": used,
            "reserved": reserved,
            "remaining": remaining,
            "plan": self.plan_code,
            "source": self.source,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


def user_id(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub") or user.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def entitlement_error(code: str, feature_key: str, message: str, data: dict[str, Any] | None = None) -> HTTPException:
    detail = {"code": code, "feature_key": feature_key, "message": message}
    if data:
        detail["data"] = data
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def quota_error(feature_key: str, message: str, data: dict[str, Any]) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "quota_exceeded", "feature_key": feature_key, "message": message, "data": data},
    )


async def _user_record(session: AsyncSession, user: dict[str, Any], *, for_update: bool = False) -> User | None:
    uid = user_id(user)
    if uid <= 0:
        return None
    statement = select(User).where(User.id == uid)
    if for_update:
        statement = statement.with_for_update()
    return (await session.execute(statement)).scalar_one_or_none()


async def get_effective_entitlement(
    session: AsyncSession,
    user: dict[str, Any],
    feature_key: str,
) -> EffectiveEntitlement:
    """以数据库当前状态计算有效授权；管理员为平台级无限制。"""
    if is_admin(user):
        return EffectiveEntitlement(feature_key, True, None, True, "ADMIN", "role")

    record = await _user_record(session, user)
    plan_code = str((record.plan_code if record else None) or user.get("plan_code") or "NORMAL").upper()
    expires_at = (record.plan_expires_at if record else None) or (record.expire_at if record else None)
    if expires_at and expires_at <= _now():
        return EffectiveEntitlement(feature_key, False, 0, False, plan_code, "expired", expires_at)

    plan = (await session.execute(select(Plan).where(Plan.code == plan_code, Plan.status == "active"))).scalar_one_or_none()
    plan_entry = None
    if plan is not None:
        plan_entry = (
            await session.execute(
                select(PlanEntitlement).where(PlanEntitlement.plan_id == plan.id, PlanEntitlement.feature_key == feature_key)
            )
        ).scalar_one_or_none()
    # 自定义套餐必须显式配置功能；未知/未配置套餐按最小权限处理，避免
    # 因回退到普通套餐而意外获得业务能力。
    defaults = DEFAULT_PLAN_ENTITLEMENTS.get(plan_code) or {}
    base = defaults.get(feature_key, {"enabled": False, "limit_value": 0, "unlimited": False})
    enabled = bool(plan_entry.enabled if plan_entry is not None else base["enabled"])
    limit_value = plan_entry.limit_value if plan_entry is not None else base["limit_value"]
    unlimited = bool(plan_entry.unlimited if plan_entry is not None else base["unlimited"])
    source = "plan"

    override = (
        await session.execute(
            select(UserEntitlementOverride).where(
                UserEntitlementOverride.user_id == user_id(user),
                UserEntitlementOverride.feature_key == feature_key,
            )
        )
    ).scalar_one_or_none()
    if override is not None and (override.expires_at is None or override.expires_at > _now()):
        if override.enabled is not None:
            enabled = bool(override.enabled)
        if override.limit_value is not None:
            limit_value = int(override.limit_value)
        if override.unlimited is not None:
            unlimited = bool(override.unlimited)
        source = "user_override"

    # 兼容旧版管理员页面维护的 account_limit。
    if feature_key == FEATURE_ACCOUNT and record is not None and record.account_limit is not None and override is None:
        enabled = True
        limit_value = int(record.account_limit)
        unlimited = False
        source = "legacy_account_limit"
    return EffectiveEntitlement(feature_key, enabled, limit_value, unlimited, plan_code, source, expires_at)


def _normalized_keyword(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def split_keywords(value: Any) -> list[str]:
    """把文本、多行输入规范化为逻辑关键词。"""
    values = str(value or "").replace("\r", "\n").split("\n")
    return [item for item in (_normalized_keyword(value) for value in values) if item]


async def _keyword_usage(session: AsyncSession, owner_id: int) -> int:
    rows = (
        await session.execute(
            select(FeatureRecord).where(
                FeatureRecord.owner_id == owner_id,
                FeatureRecord.feature == "keywords-with-item-id",
                FeatureRecord.status == "active",
            )
        )
    ).scalars().all()
    return len({_normalized_keyword((row.payload or {}).get("keyword")) for row in rows if _normalized_keyword((row.payload or {}).get("keyword"))})


async def _card_usage(session: AsyncSession, owner_id: int) -> int:
    rows = (
        await session.execute(
            select(FeatureRecord).where(
                FeatureRecord.owner_id == owner_id,
                FeatureRecord.feature == "cards",
                FeatureRecord.status == "active",
            )
        )
    ).scalars().all()
    return sum(1 for row in rows if bool((row.payload or {}).get("enabled", True)))


async def _auto_publish_usage(session: AsyncSession, owner_id: int) -> int:
    rows = (
        await session.execute(
            select(FeatureRecord).where(
                FeatureRecord.owner_id == owner_id,
                FeatureRecord.feature.in_(["product-auto-publish-targets", "product-publish-batches", "product-publish-jobs"]),
                FeatureRecord.status.in_(["active", "pending", "publishing", "success"]),
            )
        )
    ).scalars().all()
    keys = set()
    for row in rows:
        payload = row.payload or {}
        if not payload.get("auto_publish"):
            continue
        keys.add(str(payload.get("auto_target_id") or row.external_id or row.id))
    return len(keys)


async def quota_usage(session: AsyncSession, user: dict[str, Any], feature_key: str) -> int:
    if is_admin(user):
        return 0
    owner_id = user_id(user)
    if feature_key == FEATURE_ACCOUNT:
        return int((await session.execute(select(func.count()).select_from(Account).where(Account.user_id == owner_id))).scalar_one() or 0)
    if feature_key == FEATURE_CARD_AUTO_DELIVERY:
        return await _card_usage(session, owner_id)
    if feature_key == FEATURE_PRODUCT_AUTO_PUBLISH:
        return await _auto_publish_usage(session, owner_id)
    if feature_key == FEATURE_KEYWORD_REPLY:
        return await _keyword_usage(session, owner_id)
    return 0


async def reserved_usage(session: AsyncSession, user: dict[str, Any], feature_key: str) -> int:
    if is_admin(user):
        return 0
    now = _now()
    result = await session.execute(
        select(func.coalesce(func.sum(QuotaReservation.amount), 0)).where(
            QuotaReservation.user_id == user_id(user),
            QuotaReservation.feature_key == feature_key,
            QuotaReservation.status == "reserved",
            (QuotaReservation.expires_at.is_(None) | (QuotaReservation.expires_at > now)),
        )
    )
    return int(result.scalar_one() or 0)


async def quota_snapshot(session: AsyncSession, user: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for feature_key in ENTITLEMENT_FEATURES:
        entitlement = await get_effective_entitlement(session, user, feature_key)
        used = await quota_usage(session, user, feature_key)
        reserved = await reserved_usage(session, user, feature_key)
        result[feature_key] = entitlement.as_dict(used, reserved)
    return result


async def require_feature(session: AsyncSession, user: dict[str, Any], feature_key: str) -> EffectiveEntitlement:
    entitlement = await get_effective_entitlement(session, user, feature_key)
    if not entitlement.enabled:
        raise entitlement_error("feature_not_allowed", feature_key, "当前套餐未开通该功能")
    return entitlement


async def ensure_quota(
    session: AsyncSession,
    user: dict[str, Any],
    feature_key: str,
    amount: int = 1,
    *,
    usage: int | None = None,
) -> dict[str, Any]:
    entitlement = await require_feature(session, user, feature_key)
    used = await quota_usage(session, user, feature_key) if usage is None else usage
    reserved = await reserved_usage(session, user, feature_key)
    if not entitlement.unlimited and used + reserved + amount > int(entitlement.limit_value or 0):
        raise quota_error(feature_key, "已达到当前套餐配额上限", entitlement.as_dict(used, reserved))
    return entitlement.as_dict(used, reserved)


async def reserve_quota(
    session: AsyncSession,
    user: dict[str, Any],
    feature_key: str,
    *,
    amount: int = 1,
    idempotency_key: str | None = None,
    resource_key: str | None = None,
    expires_seconds: int = 900,
) -> QuotaReservation | None:
    """锁定用户行后预占配额，调用方需在同一事务中 finalize 或 rollback。"""
    if is_admin(user):
        return None
    if amount <= 0:
        return None
    await _user_record(session, user, for_update=True)
    key = idempotency_key or f"{feature_key}:{resource_key or datetime.now().timestamp()}"
    existing = (
        await session.execute(
            select(QuotaReservation).where(
                QuotaReservation.user_id == user_id(user),
                QuotaReservation.feature_key == feature_key,
                QuotaReservation.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    now = _now()
    if existing is not None and existing.status == "committed":
        return existing
    if (
        existing is not None
        and existing.status == "reserved"
        and existing.expires_at is not None
        and existing.expires_at > now
    ):
        return existing

    # Check quota only after the idempotency lookup. A retried request must be
    # able to receive its original reservation even if the current usage has
    # changed since the first attempt.
    await ensure_quota(session, user, feature_key, amount)
    if existing is not None:
        existing.resource_key = resource_key
        existing.amount = amount
        existing.status = "reserved"
        existing.expires_at = now + timedelta(seconds=expires_seconds)
        await session.flush()
        return existing
    reservation = QuotaReservation(
        user_id=user_id(user),
        feature_key=feature_key,
        resource_key=resource_key,
        idempotency_key=key,
        amount=amount,
        status="reserved",
        expires_at=now + timedelta(seconds=expires_seconds),
    )
    session.add(reservation)
    await session.flush()
    return reservation


def finalize_quota(reservation: QuotaReservation | None) -> None:
    if reservation is not None:
        reservation.status = "committed"


def release_quota(reservation: QuotaReservation | None) -> None:
    if reservation is not None:
        reservation.status = "released"


async def entitlement_payload(session: AsyncSession, user: dict[str, Any]) -> dict[str, Any]:
    plan_code = "ADMIN" if is_admin(user) else str(user.get("plan_code") or "NORMAL").upper()
    return {
        "role": str(user.get("role") or "user"),
        "plan": plan_code,
        "plan_expires_at": user.get("plan_expires_at") or user.get("expire_at"),
        "features": {
            key: (await get_effective_entitlement(session, user, key)).enabled
            for key in ENTITLEMENT_FEATURES
        },
        "quotas": await quota_snapshot(session, user),
        "permissions_version": int(user.get("auth_version") or 1),
    }
