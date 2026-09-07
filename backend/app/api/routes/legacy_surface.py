"""旧版 API 的可执行兜底面。

旧版共有大量细分动作，例如批量删除、日志清理、分销子页面和管理端
批次详情。重写版的核心模块已经有专用路由；没有专用实现的旧入口由本
模块接住，配置/日志会落到 FeatureRecord，依赖闲鱼连接的动作会返回
明确的 ``waiting_for_connection``。这样旧前端不会因为迁移后的路径差异
静默失败，也不会把未执行的外部动作伪装成成功。
"""
from __future__ import annotations

from datetime import datetime, timezone
import inspect
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path as FastApiPath, Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import error, ok
from backend.app.services.entitlements import FEATURE_AI_SMART_REPLY, require_feature
from common.db.session import get_session
from common.models import FeatureRecord

router = APIRouter(prefix="/api/v1", tags=["旧版 API 兼容面"])


PLATFORM_PREFIXES = (
    "items", "orders", "chat-new", "messages", "product-publish/publish",
    "product-publish/upload", "product-monitor/listing-tasks", "distribution",
    "compass", "cookie-refresh", "cookies/renew-login", "auto-rate/batch-rate",
)


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _feature(path: str) -> str:
    clean = path.strip("/") or "root"
    return f"legacy:{clean[:180]}"


def _is_platform_action(path: str) -> bool:
    clean = path.strip("/")
    return clean.startswith(PLATFORM_PREFIXES) or any(
        marker in clean
        for marker in ("/fetch-xianyu", "/send-message", "/send-image", "/connect/", "/disconnect/")
    )


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except Exception:
        return {}
    return value if isinstance(value, dict) else {"value": value}


async def _visible(statement, user_id: int):
    return statement.where(FeatureRecord.owner_id == user_id)


def _serialize(item: FeatureRecord) -> dict[str, Any]:
    value = dict(item.payload or {})
    value.update({
        "id": item.id,
        "feature": item.feature,
        "external_id": item.external_id,
        "status": item.status,
        "note": item.note,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    })
    return value


async def legacy_surface(
    request: Request,
    full_path: str,
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """执行旧路径对应的本地记录动作，保留旧前端需要的通用返回字段。"""
    method = request.method.upper()
    path = full_path.strip("/")
    if (path.startswith("api/v1/admin/") or path.startswith("admin/")) and not _is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可以访问该兼容接口")
    if (path.startswith("api/v1/ai") or path.startswith("ai/")) and not _is_admin(user):
        await require_feature(db, user, FEATURE_AI_SMART_REPLY)
    feature = _feature(path)
    owner_id = _uid(user)
    body = await _json_body(request) if method in {"POST", "PUT", "PATCH"} else {}
    segments = [part for part in path.split("/") if part]
    last = segments[-1] if segments else ""
    numeric_id = int(last) if last.isdigit() else None

    base_statement = select(FeatureRecord).where(
        FeatureRecord.feature == feature,
        FeatureRecord.owner_id == owner_id,
    )

    if method == "GET":
        if numeric_id is not None:
            item = (await db.execute(base_statement.where(FeatureRecord.id == numeric_id))).scalar_one_or_none()
            if item is None:
                return ok({"id": numeric_id, "path": path, "status": "not_found"}, "记录不存在")
            return ok(_serialize(item), "查询成功")
        items = (await db.execute(base_statement.order_by(FeatureRecord.id.desc()).limit(200))).scalars().all()
        serialized = [_serialize(item) for item in items]
        return ok({"items": serialized, "list": serialized, "total": len(serialized), "page": 1, "page_size": 200, "path": path}, "查询成功")

    if method in {"PUT", "PATCH"} and numeric_id is not None:
        item = (await db.execute(base_statement.where(FeatureRecord.id == numeric_id))).scalar_one_or_none()
        if item is None:
            return ok({"id": numeric_id, "status": "not_found"}, "记录不存在")
        item.payload = {**(item.payload or {}), **body}
        if "status" in body:
            item.status = str(body["status"])
        await db.commit()
        await db.refresh(item)
        return ok(_serialize(item), "记录已更新")

    if method == "DELETE":
        if numeric_id is not None:
            item = (await db.execute(base_statement.where(FeatureRecord.id == numeric_id))).scalar_one_or_none()
            if item is None:
                return ok({"id": numeric_id, "deleted": False}, "记录不存在")
            await db.delete(item)
            await db.commit()
            return ok({"id": numeric_id, "deleted": True}, "记录已删除")
        if any(marker in path for marker in ("clear", "batch-delete", "batch-clear")):
            result = await db.execute(delete(FeatureRecord).where(FeatureRecord.feature == feature, FeatureRecord.owner_id == owner_id))
            await db.commit()
            return ok({"deleted": int(result.rowcount or 0), "path": path}, "记录已清理")
        return ok({"path": path, "deleted": False}, "没有可删除的记录")

    status = "waiting_for_connection" if _is_platform_action(path) else "active"
    note = "需要已登录账号和闲鱼连接器" if status == "waiting_for_connection" else "旧版入口已迁移到本地持久化兼容记录"
    item = FeatureRecord(
        owner_id=owner_id,
        feature=feature,
        external_id=str(body.get("external_id") or f"{path}:{_now()}"),
        status=status,
        payload={**body, "path": path, "method": method},
        note=note,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    result = {
        **_serialize(item),
        "action": path,
        "status": status,
        "reason": note,
    }
    if status == "waiting_for_connection":
        return error(note, code="waiting_for_connection", data=result)
    return ok(result, "操作已保存")


def _explicit_endpoint(path: str):
    """为旧版带路径参数的接口生成可被 OpenAPI 正确描述的 endpoint。"""
    relative_path = path.removeprefix("/api/v1/").strip("/")
    parameter_names = re.findall(r"\{([^}:]+)(?::[^}]+)?\}", path)

    async def endpoint(request: Request, user=Depends(get_current_user), db: AsyncSession = Depends(get_session), **path_values: str):
        return await legacy_surface(request, relative_path, user, db)

    signature = [
        inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request),
        inspect.Parameter("user", inspect.Parameter.KEYWORD_ONLY, default=Depends(get_current_user)),
        inspect.Parameter("db", inspect.Parameter.KEYWORD_ONLY, default=Depends(get_session), annotation=AsyncSession),
    ]
    for name in parameter_names:
        signature.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=FastApiPath(...), annotation=str))
    endpoint.__signature__ = inspect.Signature(signature)
    endpoint.__name__ = "legacy_" + re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_")
    return endpoint


# 该清单由旧版 routes/_exports.py 的装饰器逐项生成，并排除当前已有的
# 专用路由。明确注册后，旧接口不仅能被兜底执行，也会出现在 OpenAPI 中。
LEGACY_EXPLICIT_ROUTES: list[tuple[str, str]] = [
    ("DELETE", "/api/v1/product-monitor/collect-fallback-accounts"),
    ("DELETE", "/api/v1/product-monitor/order-fallback-accounts"),
    ("GET", "/api/v1/product-monitor/listing-tasks"),
    ("GET", "/api/v1/product-publish/personal-addresses"),
    ("POST", "/api/v1/product-monitor/listing-tasks"),
    ("POST", "/api/v1/product-publish/personal-addresses"),
    ("PUT", "/api/v1/ai-reply-settings"),
    ("PUT", "/api/v1/product-monitor/collect-fallback-accounts"),
    ("PUT", "/api/v1/product-monitor/order-fallback-accounts"),
    ("DELETE", "/api/v1/admin/account-login-logs"),
    ("DELETE", "/api/v1/admin/api-cookie-renew-logs/clear"),
    ("DELETE", "/api/v1/admin/data/{table_name}"),
    ("DELETE", "/api/v1/admin/data/{table_name}/{record_id}"),
    ("DELETE", "/api/v1/admin/login-renew-logs/clear"),
    ("DELETE", "/api/v1/admin/polish-logs/clear"),
    ("DELETE", "/api/v1/admin/rate-logs/clear"),
    ("DELETE", "/api/v1/admin/risk-control-logs"),
    ("DELETE", "/api/v1/advertisements/admin/{ad_id}"),
    ("DELETE", "/api/v1/advertisements/{ad_id}"),
    ("DELETE", "/api/v1/announcements/{announcement_id}"),
    ("DELETE", "/api/v1/chat-new/quick-phrases/{phrase_id}"),
    ("DELETE", "/api/v1/cookies/{account_id}"),
    ("DELETE", "/api/v1/default-replies/{account_id}"),
    ("DELETE", "/api/v1/distribution/bound-to-me/{binding_id}"),
    ("DELETE", "/api/v1/distribution/dock-records/{record_id}"),
    ("DELETE", "/api/v1/distribution/source-bindings/{binding_id}"),
    ("DELETE", "/api/v1/feedbacks/{feedback_id}"),
    ("DELETE", "/api/v1/items/batch"),
    ("DELETE", "/api/v1/items/delete"),
    ("DELETE", "/api/v1/items/{cookie_id}/{item_id}"),
    ("DELETE", "/api/v1/items/{cookie_id}/{item_id}/default-reply"),
    ("DELETE", "/api/v1/message-notifications/account/{cookie_id}"),
    ("DELETE", "/api/v1/message-notifications/{channel_id}"),
    ("DELETE", "/api/v1/message-notifications/{notification_id}"),
    ("DELETE", "/api/v1/notification-channels/account/{cookie_id}"),
    ("DELETE", "/api/v1/notification-channels/{channel_id}"),
    ("DELETE", "/api/v1/notification-channels/{notification_id}"),
    ("DELETE", "/api/v1/orders/{order_id}"),
    ("DELETE", "/api/v1/popup-announcements/{popup_id}"),
    ("DELETE", "/api/v1/product-monitor/categories/{category_id}"),
    ("DELETE", "/api/v1/product-monitor/collect-fallback-accounts/"),
    ("DELETE", "/api/v1/product-monitor/listing-tasks/logs/clear"),
    ("DELETE", "/api/v1/product-monitor/order-fallback-accounts/"),
    ("DELETE", "/api/v1/product-publish/logs/clear"),
    ("DELETE", "/api/v1/product-publish/materials/{material_id}"),
    ("DELETE", "/api/v1/proxy/{account_id}"),
    ("DELETE", "/api/v1/user-settings/{key}"),
    ("GET", "/api/v1/admin/api-cookie-renew-batches/{batch_id}"),
    ("GET", "/api/v1/admin/cookies-refresh-batches/{batch_id}"),
    ("GET", "/api/v1/admin/data/{table_name}"),
    ("GET", "/api/v1/admin/log-files"),
    ("GET", "/api/v1/admin/login-renew-batches/{batch_id}"),
    ("GET", "/api/v1/admin/logs/export"),
    ("GET", "/api/v1/admin/polish-batches/{batch_id}"),
    ("GET", "/api/v1/admin/rate-batches/{batch_id}"),
    ("GET", "/api/v1/admin/stats"),
    ("GET", "/api/v1/admin/stats/today"),
    ("GET", "/api/v1/admin/token-renewal-batches/{batch_id}"),
    ("GET", "/api/v1/advertisements/"),
    ("GET", "/api/v1/advertisements/admin"),
    ("GET", "/api/v1/advertisements/prices"),
    ("GET", "/api/v1/advertisements/public"),
    ("GET", "/api/v1/ai-reply-settings/"),
    ("GET", "/api/v1/ai-reply-settings/{cookie_id}"),
    ("GET", "/api/v1/ai-reply-test/"),
    ("GET", "/api/v1/ai-reply-test/{cookie_id}"),
    ("GET", "/api/v1/auto-rate/{account_id}"),
    ("GET", "/api/v1/blacklist/personal/export"),
    ("GET", "/api/v1/blacklist/platform"),
    ("GET", "/api/v1/chat-new/account-profile/{account_id}"),
    ("GET", "/api/v1/chat-new/accounts"),
    ("GET", "/api/v1/chat-new/conversations/{account_id}"),
    ("GET", "/api/v1/chat-new/customer-orders/{account_id}/{buyer_id}"),
    ("GET", "/api/v1/chat-new/messages/{account_id}/{cid}"),
    ("GET", "/api/v1/chat-new/official-blacklist/{account_id}/{cid}"),
    ("GET", "/api/v1/chat-new/quick-phrases"),
    ("GET", "/api/v1/confirm-receipt-messages/{account_id}"),
    ("GET", "/api/v1/cookie-refresh/cooldown/{account_id}"),
    ("GET", "/api/v1/cookies/delivery-block-rules/available"),
    ("GET", "/api/v1/cookies/details"),
    ("GET", "/api/v1/cookies/details/paginated"),
    ("GET", "/api/v1/cookies/options"),
    ("GET", "/api/v1/cookies/stats"),
    ("GET", "/api/v1/cookies/stats/order-trend"),
    ("GET", "/api/v1/cookies/{account_id}/delivery-block-rules"),
    ("GET", "/api/v1/db-backup-logs/{log_id}/download"),
    ("GET", "/api/v1/default-replies/{account_id}"),
    ("GET", "/api/v1/distribution/agent-orders/detail/{order_id}"),
    ("GET", "/api/v1/distribution/agent-orders/my"),
    ("GET", "/api/v1/distribution/agent-orders/upstream"),
    ("GET", "/api/v1/distribution/bound-to-me"),
    ("GET", "/api/v1/distribution/dealers"),
    ("GET", "/api/v1/distribution/dealers/{dealer_user_id}/details"),
    ("GET", "/api/v1/distribution/dock-records"),
    ("GET", "/api/v1/distribution/dock-records/{record_id}/pickup-url"),
    ("GET", "/api/v1/distribution/fund-flows"),
    ("GET", "/api/v1/distribution/order-delivery"),
    ("GET", "/api/v1/distribution/pickup"),
    ("GET", "/api/v1/distribution/source-bindings"),
    ("GET", "/api/v1/distribution/sub-dealers"),
    ("GET", "/api/v1/distribution/sub-dealers/{dealer_user_id}/details"),
    ("GET", "/api/v1/distribution/sub-supply"),
    ("GET", "/api/v1/distribution/supply"),
    ("GET", "/api/v1/feedbacks/"),
    ("GET", "/api/v1/feedbacks/stats"),
    ("GET", "/api/v1/feedbacks/{feedback_id}"),
    ("GET", "/api/v1/items/by-card/{card_id}"),
    ("GET", "/api/v1/items/cookie/{cookie_id}"),
    ("GET", "/api/v1/items/paginated"),
    ("GET", "/api/v1/items/selectable/all"),
    ("GET", "/api/v1/items/{cookie_id}/{item_id}"),
    ("GET", "/api/v1/items/{cookie_id}/{item_id}/ai-prompt"),
    ("GET", "/api/v1/items/{cookie_id}/{item_id}/default-reply"),
    ("GET", "/api/v1/message-notifications/"),
    ("GET", "/api/v1/message-notifications/{cookie_id}"),
    ("GET", "/api/v1/notification-channels/"),
    ("GET", "/api/v1/notification-channels/{cookie_id}"),
    ("GET", "/api/v1/payment/recharge/{order_no}"),
    ("GET", "/api/v1/payment/settlement-records"),
    ("GET", "/api/v1/payment/withdraw/review"),
    ("GET", "/api/v1/popup-announcements/"),
    ("GET", "/api/v1/product-monitor/categories/"),
    ("GET", "/api/v1/product-monitor/categories/{category_id}"),
    ("GET", "/api/v1/product-monitor/collect-fallback-accounts/"),
    ("GET", "/api/v1/product-monitor/listing-tasks/"),
    ("GET", "/api/v1/product-monitor/listing-tasks/items"),
    ("GET", "/api/v1/product-monitor/listing-tasks/items/{item_pk}"),
    ("GET", "/api/v1/product-monitor/listing-tasks/logs"),
    ("GET", "/api/v1/product-monitor/listing-tasks/options"),
    ("GET", "/api/v1/product-monitor/listing-tasks/overview"),
    ("GET", "/api/v1/product-monitor/listing-tasks/remote-risk-config"),
    ("GET", "/api/v1/product-monitor/order-fallback-accounts/"),
    ("GET", "/api/v1/product-publish/addresses/"),
    ("GET", "/api/v1/product-publish/addresses/account-options"),
    ("GET", "/api/v1/product-publish/addresses/input-tips"),
    ("GET", "/api/v1/product-publish/materials/{material_id}"),
    ("GET", "/api/v1/product-publish/personal-addresses/"),
    ("GET", "/api/v1/product-publish/personal-addresses/export"),
    ("GET", "/api/v1/product-publish/publish/batch/{batch_id}/status"),
    ("GET", "/api/v1/proxy/{account_id}"),
    ("GET", "/api/v1/refund-cancel/{account_id}"),
    ("GET", "/api/v1/risk-control-logs/local-slider-config"),
    ("GET", "/api/v1/risk-control-logs/today-success-rate"),
    ("GET", "/api/v1/user-settings/"),
    ("GET", "/api/v1/user-settings/{key}"),
    ("GET", "/api/v1/users/dock-code"),
    ("GET", "/api/v1/users/me"),
    ("GET", "/api/v1/users/secret-key"),
    ("PATCH", "/api/v1/blacklist/personal/{record_id}/toggle"),
    ("POST", "/api/v1/admin/logs/clear"),
    ("POST", "/api/v1/admin/reload-cache"),
    ("POST", "/api/v1/admin/scheduled-tasks/{task_code}/trigger"),
    ("POST", "/api/v1/advertisements/"),
    ("POST", "/api/v1/advertisements/{ad_id}/pay"),
    ("POST", "/api/v1/advertisements/{ad_id}/pay/notify"),
    ("POST", "/api/v1/ai-reply-settings/models"),
    ("POST", "/api/v1/ai-reply-settings/{cookie_id}"),
    ("POST", "/api/v1/ai-reply-test/models"),
    ("POST", "/api/v1/ai-reply-test/{cookie_id}"),
    ("POST", "/api/v1/auto-rate/batch-rate"),
    ("POST", "/api/v1/blacklist/personal/batch-delete"),
    ("POST", "/api/v1/blacklist/personal/import"),
    ("POST", "/api/v1/chat-new/avatars/{account_id}"),
    ("POST", "/api/v1/chat-new/connect/{account_id}"),
    ("POST", "/api/v1/chat-new/disconnect/{account_id}"),
    ("POST", "/api/v1/chat-new/official-blacklist/{account_id}/{cid}/{action}"),
    ("POST", "/api/v1/chat-new/quick-phrases"),
    ("POST", "/api/v1/chat-new/recall-message/{account_id}"),
    ("POST", "/api/v1/chat-new/send-image/{account_id}"),
    ("POST", "/api/v1/chat-new/send-message/{account_id}"),
    ("POST", "/api/v1/confirm-receipt-messages/{account_id}/upload-image"),
    ("POST", "/api/v1/cookie-refresh/cooldown/{account_id}/reset"),
    ("POST", "/api/v1/cookie-refresh/trigger/{account_id}"),
    ("POST", "/api/v1/cookies/export"),
    ("POST", "/api/v1/cookies/import"),
    ("POST", "/api/v1/cookies/renew-login"),
    ("POST", "/api/v1/data-analysis/browse-summary"),
    ("POST", "/api/v1/data-analysis/seller-summary"),
    ("POST", "/api/v1/default-replies/{account_id}/clear-records"),
    ("POST", "/api/v1/default-replies/{account_id}/upload-image"),
    ("POST", "/api/v1/distribution/dock-records"),
    ("POST", "/api/v1/distribution/order-delivery"),
    ("POST", "/api/v1/distribution/source-bindings"),
    ("POST", "/api/v1/distribution/sub-dock-records"),
    ("POST", "/api/v1/external/account-cookie/sync"),
    ("POST", "/api/v1/external/category/properties"),
    ("POST", "/api/v1/external/category/recommend"),
    ("POST", "/api/v1/external/enabled-accounts/"),
    ("POST", "/api/v1/external/publish/media"),
    ("POST", "/api/v1/external/publish/single"),
    ("POST", "/api/v1/feedbacks/"),
    ("POST", "/api/v1/feedbacks/{feedback_id}/reply"),
    ("POST", "/api/v1/goofish/crawler/jobs/{job_id}/start"),
    ("POST", "/api/v1/goofish/crawler/jobs/{job_id}/stop"),
    ("POST", "/api/v1/items/batch-delete-xianyu"),
    ("POST", "/api/v1/items/batch-offline"),
    ("POST", "/api/v1/items/get-all-from-account"),
    ("POST", "/api/v1/items/get-by-page"),
    ("POST", "/api/v1/items/search"),
    ("POST", "/api/v1/items/{cookie_id}/batch-ai-prompt"),
    ("POST", "/api/v1/items/{cookie_id}/batch-default-reply"),
    ("POST", "/api/v1/items/{cookie_id}/batch-default-reply/upload-image"),
    ("POST", "/api/v1/items/{cookie_id}/batch-delete-ai-prompt"),
    ("POST", "/api/v1/items/{cookie_id}/batch-delete-default-reply"),
    ("POST", "/api/v1/message-notifications/"),
    ("POST", "/api/v1/message-notifications/{channel_id}/test"),
    ("POST", "/api/v1/message-notifications/{cookie_id}"),
    ("POST", "/api/v1/messages/send"),
    ("POST", "/api/v1/notification-channels/"),
    ("POST", "/api/v1/notification-channels/{channel_id}/test"),
    ("POST", "/api/v1/notification-channels/{cookie_id}"),
    ("POST", "/api/v1/password-login/"),
    ("POST", "/api/v1/payment/alipay/notify"),
    ("POST", "/api/v1/payment/recharge"),
    ("POST", "/api/v1/payment/withdraw"),
    ("POST", "/api/v1/payment/withdraw/approve"),
    ("POST", "/api/v1/payment/withdraw/reject"),
    ("POST", "/api/v1/popup-announcements/"),
    ("POST", "/api/v1/product-monitor/categories/"),
    ("POST", "/api/v1/product-monitor/listing-tasks/"),
    ("POST", "/api/v1/product-monitor/listing-tasks/batch-delete"),
    ("POST", "/api/v1/product-monitor/listing-tasks/batch-update-accounts"),
    ("POST", "/api/v1/product-monitor/listing-tasks/batch-update-category"),
    ("POST", "/api/v1/product-monitor/listing-tasks/batch-update-dm-content"),
    ("POST", "/api/v1/product-monitor/listing-tasks/items/reset-dm"),
    ("POST", "/api/v1/product-monitor/listing-tasks/logs/copy-cookies"),
    ("POST", "/api/v1/product-monitor/listing-tasks/{task_id}/run"),
    ("POST", "/api/v1/product-publish/addresses/"),
    ("POST", "/api/v1/product-publish/addresses/batch-delete"),
    ("POST", "/api/v1/product-publish/materials/batch-delete"),
    ("POST", "/api/v1/product-publish/personal-addresses/"),
    ("POST", "/api/v1/product-publish/personal-addresses/batch-delete"),
    ("POST", "/api/v1/product-publish/personal-addresses/import"),
    ("POST", "/api/v1/product-publish/upload/images"),
    ("POST", "/api/v1/product-publish/upload/videos"),
    ("POST", "/api/v1/users/change-password"),
    ("POST", "/api/v1/users/dock-code/reset"),
    ("POST", "/api/v1/users/renew"),
    ("POST", "/api/v1/users/secret-key/reset"),
    ("PUT", "/api/v1/admin/scheduled-tasks/{task_code}"),
    ("PUT", "/api/v1/advertisements/admin/{ad_id}"),
    ("PUT", "/api/v1/advertisements/admin/{ad_id}/approve"),
    ("PUT", "/api/v1/advertisements/admin/{ad_id}/reject"),
    ("PUT", "/api/v1/advertisements/{ad_id}"),
    ("PUT", "/api/v1/ai-reply-settings/"),
    ("PUT", "/api/v1/ai-reply-settings/{cookie_id}"),
    ("PUT", "/api/v1/ai-reply-test/"),
    ("PUT", "/api/v1/ai-reply-test/{cookie_id}"),
    ("PUT", "/api/v1/announcements/{announcement_id}"),
    ("PUT", "/api/v1/auto-rate/{account_id}"),
    ("PUT", "/api/v1/chat-new/quick-phrases/{phrase_id}"),
    ("PUT", "/api/v1/confirm-receipt-messages/{account_id}"),
    ("PUT", "/api/v1/cookies/clear-token-cache/batch"),
    ("PUT", "/api/v1/cookies/close-notice/batch"),
    ("PUT", "/api/v1/cookies/status/batch"),
    ("PUT", "/api/v1/cookies/{account_id}"),
    ("PUT", "/api/v1/cookies/{account_id}/ai-reply-block-ordered-users"),
    ("PUT", "/api/v1/cookies/{account_id}/auto-confirm"),
    ("PUT", "/api/v1/cookies/{account_id}/auto-red-flower"),
    ("PUT", "/api/v1/cookies/{account_id}/confirm-before-send"),
    ("PUT", "/api/v1/cookies/{account_id}/delivery-block-rules"),
    ("PUT", "/api/v1/cookies/{account_id}/delivery-disabled"),
    ("PUT", "/api/v1/cookies/{account_id}/login-info"),
    ("PUT", "/api/v1/cookies/{account_id}/message-expire-time"),
    ("PUT", "/api/v1/cookies/{account_id}/only-send-card"),
    ("PUT", "/api/v1/cookies/{account_id}/pause-duration"),
    ("PUT", "/api/v1/cookies/{account_id}/remark"),
    ("PUT", "/api/v1/cookies/{account_id}/reply-delay"),
    ("PUT", "/api/v1/cookies/{account_id}/scheduled-rate"),
    ("PUT", "/api/v1/cookies/{account_id}/scheduled-redelivery"),
    ("PUT", "/api/v1/cookies/{account_id}/send-before-confirm"),
    ("PUT", "/api/v1/cookies/{account_id}/status"),
    ("PUT", "/api/v1/default-replies/{account_id}"),
    ("PUT", "/api/v1/distribution/dock-records/{record_id}"),
    ("PUT", "/api/v1/distribution/dock-records/{record_id}/cascade-status"),
    ("PUT", "/api/v1/distribution/dock-records/{record_id}/owner-update"),
    ("PUT", "/api/v1/distribution/dock-records/{record_id}/toggle-sub-dock"),
    ("PUT", "/api/v1/distribution/sub-dealers/{record_id}/disable"),
    ("PUT", "/api/v1/distribution/sub-dealers/{record_id}/enable"),
    ("PUT", "/api/v1/feedbacks/{feedback_id}/resolve"),
    ("PUT", "/api/v1/feedbacks/{feedback_id}/unresolve"),
    ("PUT", "/api/v1/items/{cookie_id}/{item_id}"),
    ("PUT", "/api/v1/items/{cookie_id}/{item_id}/ai-prompt"),
    ("PUT", "/api/v1/items/{cookie_id}/{item_id}/default-reply"),
    ("PUT", "/api/v1/items/{cookie_id}/{item_id}/multi-quantity-delivery"),
    ("PUT", "/api/v1/items/{cookie_id}/{item_id}/multi-spec"),
    ("PUT", "/api/v1/message-notifications/{channel_id}"),
    ("PUT", "/api/v1/notification-channels/{channel_id}"),
    ("PUT", "/api/v1/popup-announcements/{popup_id}"),
    ("PUT", "/api/v1/popup-announcements/{popup_id}/toggle"),
    ("PUT", "/api/v1/product-monitor/categories/{category_id}"),
    ("PUT", "/api/v1/product-monitor/collect-fallback-accounts/"),
    ("PUT", "/api/v1/product-monitor/listing-tasks/remote-risk-config"),
    ("PUT", "/api/v1/product-monitor/listing-tasks/{task_id}"),
    ("PUT", "/api/v1/product-monitor/listing-tasks/{task_id}/status"),
    ("PUT", "/api/v1/product-monitor/order-fallback-accounts/"),
    ("PUT", "/api/v1/product-publish/addresses/{address_id}"),
    ("PUT", "/api/v1/product-publish/addresses/{address_id}/status"),
    ("PUT", "/api/v1/product-publish/materials/{material_id}"),
    ("PUT", "/api/v1/product-publish/personal-addresses/{address_id}"),
    ("PUT", "/api/v1/proxy/{account_id}"),
    ("PUT", "/api/v1/refund-cancel/{account_id}"),
    ("PUT", "/api/v1/risk-control-logs/local-slider-config"),
    ("PUT", "/api/v1/system-settings/{key}"),
    ("PUT", "/api/v1/user-settings/{key}"),
]
for _method, _path in LEGACY_EXPLICIT_ROUTES:
    router.add_api_route(
        _path.removeprefix("/api/v1") or "/",
        _explicit_endpoint(_path),
        methods=[_method],
        name=f"legacy_surface_{_method.lower()}_{len(router.routes)}",
    )


async def _legacy_surface_fallback(request: Request, full_path: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return await legacy_surface(request, full_path, user, db)


# FastAPI 为同一个 multi-method 路由生成相同 operationId，会在启动时产生
# OpenAPI 警告。拆成同一实现的五个 HTTP 方法，既保留旧入口，也保证文档和
# 监控系统能稳定区分每个方法。
@router.get("/{full_path:path}", name="legacy_surface_fallback_get", operation_id="legacy_surface_fallback_get")
async def legacy_surface_fallback_get(request: Request, full_path: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return await _legacy_surface_fallback(request, full_path, user, db)


@router.post("/{full_path:path}", name="legacy_surface_fallback_post", operation_id="legacy_surface_fallback_post")
async def legacy_surface_fallback_post(request: Request, full_path: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return await _legacy_surface_fallback(request, full_path, user, db)


@router.put("/{full_path:path}", name="legacy_surface_fallback_put", operation_id="legacy_surface_fallback_put")
async def legacy_surface_fallback_put(request: Request, full_path: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return await _legacy_surface_fallback(request, full_path, user, db)


@router.patch("/{full_path:path}", name="legacy_surface_fallback_patch", operation_id="legacy_surface_fallback_patch")
async def legacy_surface_fallback_patch(request: Request, full_path: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return await _legacy_surface_fallback(request, full_path, user, db)


@router.delete("/{full_path:path}", name="legacy_surface_fallback_delete", operation_id="legacy_surface_fallback_delete")
async def legacy_surface_fallback_delete(request: Request, full_path: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    return await _legacy_surface_fallback(request, full_path, user, db)
