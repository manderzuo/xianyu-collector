# -*- coding: utf-8 -*-
"""backend-web：统一 API、鉴权、上传与静态文件入口。"""
from __future__ import annotations

import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi import HTTPException
from sqlalchemy import text

from common.config import settings
from common.db.session import async_session_maker, init_db
from backend.app.core.response import ok
from backend.app.api.routes.auth import router as auth_router
from backend.app.api.routes.user_profile import router as user_profile_router
from backend.app.api.routes.admin_users import router as admin_users_router
from backend.app.api.routes.registration_invites import router as registration_invites_router
from backend.app.api.routes.admin_entitlements import router as admin_entitlements_router
from backend.app.api.routes.cards import router as cards_router
from backend.app.api.routes.keywords import router as keywords_router
from backend.app.api.routes.message_filters import router as message_filters_router
from backend.app.api.routes.generic import build_resource_router
from backend.app.api.routes.upload import router as upload_router
from backend.app.api.routes.product_publish_upload import router as product_publish_upload_router
from backend.app.api.routes.product_publish import router as product_publish_router
from backend.app.api.routes.accounts import router as accounts_router
from backend.app.api.routes.account_compat import router as account_compat_router
from backend.app.api.routes.orders_compat import router as orders_compat_router
from backend.app.api.routes.account_contents import router as account_contents_router
from backend.app.api.routes.scheduled_tasks import admin_router as admin_scheduled_router
from backend.app.api.routes.scheduled_tasks import router as scheduled_router
from backend.app.api.routes.ranking import router as ranking_router
from backend.app.api.routes.analytics import router as analytics_router
from backend.app.api.routes.qr_login import router as qr_login_router
from backend.app.api.routes.search import router as search_router
from backend.app.api.routes.goofish_crawler import router as goofish_crawler_router
from backend.app.api.routes.user_settings_compat import router as user_settings_compat_router
from backend.app.api.routes.legacy_compat import router as legacy_compat_router
from backend.app.api.routes.shared_scan import router as shared_scan_router
from backend.app.api.routes.face_verification import router as face_verification_router
from backend.app.api.routes.legacy_surface import router as legacy_surface_router
from backend.app.api.routes.system_settings import router as system_settings_router
from backend.app.api.routes.chat_new import router as chat_new_router
from backend.app.api.routes.notifications import router as notifications_router
from backend.app.api.routes.risk_control import router as risk_control_router
from backend.app.api.routes.risk_control_admin import router as risk_control_admin_router
from backend.app.api.routes.polish_logs import router as polish_logs_router
from backend.app.api.routes.ai_reply import router as ai_reply_router
from backend.app.api.routes.auto_rate import router as auto_rate_router
from backend.app.api.routes.maintenance import router as maintenance_router
from backend.app.api.routes.listing_monitor import router as listing_monitor_router
from backend.app.api.routes.compass import router as compass_router
from backend.app.api.routes.distribution import router as distribution_router
from backend.app.api.routes.card_dock import router as card_dock_router
from backend.app.api.routes.advertisements import router as advertisements_router
from backend.app.api.routes.announcement_content import announcement_router, popup_router
from backend.app.api.routes.payment import router as payment_router
from backend.app.api.routes.monitor_support import category_router, collect_router, order_router
from backend.app.api.routes.personal_addresses import router as personal_addresses_router
from backend.app.api.routes.feedbacks import router as feedbacks_router
from backend.app.api.routes.blacklist import router as blacklist_router
from backend.app.api.routes.internal_tasks import router as internal_tasks_router
from backend.app.api.routes.redelivery_logs import router as redelivery_logs_router
from backend.app.api.routes.scheduled_feature_logs import router as scheduled_feature_logs_router
from backend.app.api.routes.captcha import router as captcha_router
from backend.app.api.routes.geetest import router as geetest_router
from backend.app.api.routes.admin_backup import router as admin_backup_router
from backend.app.api.routes.qrcode import router as qrcode_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("xr.backend")
app = FastAPI(title=f"{settings.brand_name} API", version="1.0.6", docs_url="/docs", redoc_url="/redoc")

origins = [item.strip() for item in settings.cors_origins.split(",") if item.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins or ["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

Path(settings.static_dir).mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")


@app.on_event("startup")
async def startup() -> None:
    try:
        await init_db()
        logger.info("database schema initialized")
    except Exception as exc:  # pragma: no cover - 本地无 MySQL 时仍可启动健康检查
        logger.warning("database initialization skipped: %s", exc)


@app.get("/health", tags=["系统"])
async def health():
    try:
        async with async_session_maker() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("health check database failed: %s", exc)
        raise HTTPException(status_code=503, detail="数据库暂不可用") from exc
    return ok({"service": "backend-web", "status": "running", "database": "ready"})


@app.get("/api/v1/health", tags=["系统"])
async def api_health():
    """供前端同源探活的健康别名，真实探针仍使用根路径。"""
    return ok({"service": "backend-web", "status": "running"})


app.include_router(auth_router)
app.include_router(user_profile_router)
app.include_router(admin_users_router)
app.include_router(registration_invites_router)
app.include_router(admin_entitlements_router)
app.include_router(cards_router)
app.include_router(keywords_router)
app.include_router(message_filters_router)
app.include_router(upload_router)
app.include_router(product_publish_upload_router)
app.include_router(product_publish_router)
app.include_router(accounts_router)
app.include_router(account_compat_router)
app.include_router(orders_compat_router)
app.include_router(account_contents_router)
app.include_router(scheduled_router)
app.include_router(admin_scheduled_router)
app.include_router(ranking_router)
app.include_router(analytics_router)
app.include_router(qr_login_router)
app.include_router(search_router)
app.include_router(goofish_crawler_router)
app.include_router(user_settings_compat_router)
app.include_router(chat_new_router)
app.include_router(notifications_router)
app.include_router(risk_control_router)
app.include_router(risk_control_admin_router)
app.include_router(polish_logs_router)
app.include_router(ai_reply_router)
app.include_router(auto_rate_router)
app.include_router(maintenance_router)
app.include_router(listing_monitor_router)
app.include_router(compass_router)
app.include_router(distribution_router)
app.include_router(card_dock_router)
app.include_router(advertisements_router)
app.include_router(announcement_router)
app.include_router(popup_router)
app.include_router(payment_router)
app.include_router(category_router)
app.include_router(collect_router)
app.include_router(order_router)
app.include_router(personal_addresses_router)
app.include_router(feedbacks_router)
app.include_router(blacklist_router)
app.include_router(redelivery_logs_router)
app.include_router(scheduled_feature_logs_router)
app.include_router(captcha_router)
app.include_router(geetest_router)
app.include_router(admin_backup_router)
app.include_router(qrcode_router)
app.include_router(legacy_compat_router)
app.include_router(shared_scan_router)
app.include_router(face_verification_router)
# 特殊系统设置键必须先于旧接口兼容兜底层注册，否则
# /system-settings/token.* 会被当成 FeatureRecord 而不会真正写入设置表。
app.include_router(system_settings_router)
app.include_router(internal_tasks_router)
app.include_router(legacy_surface_router)

RESOURCE_ROUTES = [
    ("/api/v1/users", "用户管理"), ("/api/v1/system-settings", "系统设置"),
    ("/api/v1/cookies", "Cookie 维护"),
    ("/api/v1/qr", "扫码登录"), ("/api/v1/proxy", "代理管理"),
    ("/api/v1/messages", "消息记录"), ("/api/v1/keywords", "关键词规则"),
    ("/api/v1/default-replies", "默认回复"), ("/api/v1/chat", "在线聊天"),
    ("/api/v1/ai", "AI 配置"), ("/api/v1/materials", "素材库"),
    ("/api/v1/publish", "商品发布"), ("/api/v1/publish-addresses", "地址库"),
    ("/api/v1/publish-capability", "发布能力"),
    ("/api/v1/auto-rate", "自动评价"), ("/api/v1/refund-cancel", "退款与取消"),
    ("/api/v1/goofish", "采集中心"), ("/api/v1/listing-monitor", "商品监控"),
    ("/api/v1/distribution", "分销管理"), ("/api/v1/compass", "指南针"),
    ("/api/v1/notifications", "通知中心"), ("/api/v1/risk-logs", "风控日志"),
    ("/api/v1/announcements", "系统公告"), ("/api/v1/popup", "弹窗管理"),
    ("/api/v1/feedback", "用户反馈"),
    ("/api/v1/external", "外部对接"), ("/api/v1/shared-scan", "共享扫码"),
    ("/api/v1/face-verification", "人脸核验"), ("/api/v1/payment", "支付配置"),
]
for route_prefix, label in RESOURCE_ROUTES:
    app.include_router(build_resource_router(route_prefix, label))
