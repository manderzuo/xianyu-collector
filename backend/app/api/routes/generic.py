# -*- coding: utf-8 -*-
"""面向业务资源的真实数据库 CRUD 路由。

不同模块共享分页、权限范围和审计字段处理，但每个 API 都落到自己的
SQLAlchemy 表，不再返回固定的演示数组。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from common.db.session import get_session
from common.models import (
    AccountCookie, AiProvider, Announcement, AutoRateRule, CapabilityCheck,
    CompassMetric, CrawlItem, DefaultReply, DistributionItem, ExternalConnection,
    FaceVerification, Feedback, KeywordRule, Material, Message, MonitorTask,
    Notification, Order, PaymentRecord, Popup, ProxyEndpoint, PublishAddress,
    PublishJob, QrSession, RankingEntry, RefundCase, RiskLog, SharedScanSession,
    SystemSetting, User,
)
from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.core.security import hash_password
from backend.app.services.entitlements import FEATURE_AI_SMART_REPLY, require_feature


RESOURCE_MODELS: dict[str, type] = {
    "users": User, "system-settings": SystemSetting, "cookies": AccountCookie,
    "qr": QrSession, "proxy": ProxyEndpoint, "messages": Message, "chat": Message,
    "keywords": KeywordRule, "default-replies": DefaultReply, "ai": AiProvider,
    "materials": Material, "publish": PublishJob, "publish-addresses": PublishAddress,
    "publish-capability": CapabilityCheck, "orders": Order, "auto-rate": AutoRateRule,
    "refund-cancel": RefundCase, "goofish": CrawlItem, "listing-monitor": MonitorTask,
    "distribution": DistributionItem, "compass": CompassMetric, "notifications": Notification,
    "risk-logs": RiskLog, "announcements": Announcement, "popup": Popup,
    "feedback": Feedback, "external": ExternalConnection, "shared-scan": SharedScanSession,
    "face-verification": FaceVerification, "payment": PaymentRecord, "ranking": RankingEntry,
}

READ_ONLY = {"id", "created_at", "updated_at", "checked_at"}
HIDDEN_INPUT = {"password_hash", "api_key_encrypted"}


def _serialize_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def serialize_model(item: Any) -> dict[str, Any]:
    return {
        column.name: _serialize_value(getattr(item, column.name))
        for column in item.__table__.columns
        if column.name not in {"password_hash", "api_key"}
    }


def _user_id(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _admin_only_resource(resource: str, user: dict[str, Any]) -> None:
    model = RESOURCE_MODELS.get(resource)
    columns = {column.name for column in model.__table__.columns} if model is not None else set()
    # 没有租户归属字段的资源无法安全按用户隔离，只允许管理员访问。
    if (resource in {"users", "system-settings"} or not ({"owner_id", "user_id"} & columns)) and not _is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可以访问该资源")


async def _require_resource_feature(resource: str, user: dict[str, Any], session: AsyncSession) -> None:
    if resource == "ai" and not _is_admin(user):
        await require_feature(session, user, FEATURE_AI_SMART_REPLY)


def _defaults(resource: str, payload: dict[str, Any], user_id: int) -> dict[str, Any]:
    """为可直接从 UI 创建的资源补齐业务最小字段。"""
    data = dict(payload)
    if resource == "users":
        data.setdefault("username", f"user_{uuid4().hex[:8]}")
        data["password_hash"] = hash_password(str(data.pop("password", "admin123")))
        data.setdefault("role", "user"); data.setdefault("status", 1)
    if resource == "system-settings": data.setdefault("setting_key", f"setting.{uuid4().hex[:8]}")
    if resource == "cookies": data.setdefault("account_id", 0); data.setdefault("cookie_value", "pending")
    if resource == "qr": data.setdefault("qr_token", uuid4().hex); data.setdefault("expires_at", datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5))
    if resource == "proxy": data.setdefault("name", "未命名代理"); data.setdefault("url", "")
    if resource in {"messages", "chat"}:
        data.setdefault("account_id", 0); data.setdefault("peer_id", "unknown"); data.setdefault("direction", "out"); data.setdefault("content", "")
    if resource == "keywords": data.setdefault("keyword", ""); data.setdefault("reply", ""); data.setdefault("status", 1)
    if resource == "default-replies": data.setdefault("title", "快捷回复"); data.setdefault("content", ""); data.setdefault("status", 1)
    if resource == "ai": data.setdefault("provider", "custom"); data.setdefault("model", "default")
    if resource == "materials": data.setdefault("name", "未命名素材"); data.setdefault("url", "")
    if resource == "publish": data.setdefault("title", "未命名商品"); data.setdefault("price", 0)
    if resource == "publish-addresses": data.setdefault("name", "默认地址")
    if resource == "publish-capability": data.setdefault("capability", "publish")
    if resource == "orders": data.setdefault("account_id", 0); data.setdefault("order_no", f"XR-{uuid4().hex[:16]}"); data.setdefault("amount", 0)
    if resource == "auto-rate": data.setdefault("name", "默认评价"); data.setdefault("content", "交易愉快，感谢支持")
    if resource == "refund-cancel": data.setdefault("reason", "待补充原因")
    if resource == "goofish": data.setdefault("source", "manual"); data.setdefault("external_id", uuid4().hex); data.setdefault("title", "待采集商品")
    if resource == "listing-monitor": data.setdefault("name", "新监控任务"); data.setdefault("query", "")
    if resource == "distribution": data.setdefault("source_id", uuid4().hex); data.setdefault("title", "待分销商品")
    if resource == "compass": data.setdefault("metric_name", "custom"); data.setdefault("period", "today")
    if resource == "notifications": data.setdefault("channel", "in_app"); data.setdefault("title", "系统通知")
    if resource == "risk-logs": data.setdefault("action", "manual_review")
    if resource == "announcements": data.setdefault("title", "系统公告"); data.setdefault("content", "")
    if resource == "popup": data.setdefault("title", "工作台提示"); data.setdefault("content", "")
    if resource == "feedback": data.setdefault("content", "")
    if resource == "external": data.setdefault("provider", "custom")
    if resource == "shared-scan": data.setdefault("session_token", uuid4().hex); data.setdefault("expires_at", datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10))
    if resource == "face-verification": data.setdefault("status", "pending")
    if resource == "payment": data.setdefault("provider", "manual"); data.setdefault("amount", 0)
    if resource == "ranking": data.setdefault("title", "待评估新品")
    if resource in {"keywords", "default-replies", "auto-rate", "popup"} and isinstance(data.get("status"), str):
        data["status"] = 1 if data["status"] in {"active", "enabled", "1"} else 0
    if resource == "users" and isinstance(data.get("status"), str):
        data["status"] = 1 if data["status"] in {"active", "enabled", "1"} else 0
    columns = {column.name for column in RESOURCE_MODELS[resource].__table__.columns}
    data = {
        key: value for key, value in data.items()
        if key in columns and key not in READ_ONLY
        and (key not in HIDDEN_INPUT or (resource == "users" and key == "password_hash"))
    }
    if "owner_id" in columns: data["owner_id"] = user_id
    if "user_id" in columns: data["user_id"] = user_id
    return data


def build_resource_router(prefix: str, label: str) -> APIRouter:
    resource = prefix.rstrip("/").split("/")[-1]
    model = RESOURCE_MODELS.get(resource)
    if model is None:
        raise ValueError(f"未配置资源模型: {resource}")
    router = APIRouter(prefix=prefix, tags=[label])

    def scope(statement, user_id: int):
        columns = {column.name for column in model.__table__.columns}
        if "owner_id" in columns: return statement.where(model.owner_id == user_id)
        if "user_id" in columns: return statement.where(model.user_id == user_id)
        return statement

    @router.get("")
    @router.get("/")
    async def list_resource(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        _admin_only_resource(resource, user)
        await _require_resource_feature(resource, user, session)
        user_id = _user_id(user)
        result = await session.execute(scope(select(model).order_by(model.id.desc()), user_id).offset((page - 1) * page_size).limit(page_size))
        items = [serialize_model(item) for item in result.scalars().all()]
        count_result = await session.execute(scope(select(func.count()).select_from(model), user_id))
        return ok({"items": items, "total": int(count_result.scalar_one()), "page": page, "page_size": page_size, "resource": resource})

    @router.post("")
    @router.post("/")
    async def create_resource(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        _admin_only_resource(resource, user)
        await _require_resource_feature(resource, user, session)
        item = model(**_defaults(resource, payload or {}, _user_id(user)))
        session.add(item)
        try:
            await session.commit(); await session.refresh(item)
        except SQLAlchemyError as exc:
            await session.rollback(); raise HTTPException(409, f"{label}保存失败，请检查字段或唯一约束") from exc
        return ok(serialize_model(item), f"{label}已创建")

    @router.get("/{item_id}")
    async def get_resource(item_id: str, user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        _admin_only_resource(resource, user)
        await _require_resource_feature(resource, user, session)
        if not item_id.isdigit():
            result = await session.execute(scope(select(model).order_by(model.id.desc()).limit(20), _user_id(user)))
            return ok({"path": item_id, "items": [serialize_model(item) for item in result.scalars().all()], "resource": resource})
        item = (await session.execute(scope(select(model).where(model.id == int(item_id)), _user_id(user)))).scalar_one_or_none()
        if item is None: raise HTTPException(404, f"{label}不存在")
        return ok(serialize_model(item))

    @router.put("/{item_id}")
    async def update_resource(item_id: int, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        _admin_only_resource(resource, user)
        await _require_resource_feature(resource, user, session)
        item = (await session.execute(scope(select(model).where(model.id == item_id), _user_id(user)))).scalar_one_or_none()
        if item is None: raise HTTPException(404, f"{label}不存在")
        columns = {column.name for column in model.__table__.columns}
        for key, value in (payload or {}).items():
            if key in columns and key not in READ_ONLY and key not in HIDDEN_INPUT and key not in {"owner_id", "user_id"}: setattr(item, key, value)
        try:
            await session.commit(); await session.refresh(item)
        except SQLAlchemyError as exc:
            await session.rollback(); raise HTTPException(409, f"{label}更新失败") from exc
        return ok(serialize_model(item), f"{label}已更新")

    @router.delete("/{item_id}")
    async def delete_resource(item_id: int, user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        _admin_only_resource(resource, user)
        await _require_resource_feature(resource, user, session)
        item = (await session.execute(scope(select(model).where(model.id == item_id), _user_id(user)))).scalar_one_or_none()
        if item is None: raise HTTPException(404, f"{label}不存在")
        await session.delete(item); await session.commit()
        return ok({"id": item_id, "deleted": True}, f"{label}已删除")

    @router.get("/{subpath:path}")
    async def nested_resource(subpath: str, user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        _admin_only_resource(resource, user)
        await _require_resource_feature(resource, user, session)
        result = await session.execute(scope(select(model).order_by(model.id.desc()).limit(20), _user_id(user)))
        return ok({"path": subpath, "items": [serialize_model(item) for item in result.scalars().all()], "resource": resource})

    @router.post("/{subpath:path}")
    async def nested_action(subpath: str, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        """业务动作入口：动作会落库，外部平台未配置时明确返回待接入状态。"""
        _admin_only_resource(resource, user)
        await _require_resource_feature(resource, user, session)
        data = payload or {}
        uid = _user_id(user)
        if resource in {"chat", "messages"} and subpath in {"send", "message", "reply"}:
            item = Message(account_id=int(data.get("account_id", 0)), peer_id=str(data.get("peer_id", "unknown")), direction="out", content=str(data.get("content", "")), msg_type=str(data.get("msg_type", "text")))
            session.add(item); await session.commit(); await session.refresh(item)
            return ok(serialize_model(item), "消息已写入发送队列")
        if resource == "qr" and subpath in {"generate", "create"}:
            item = QrSession(owner_id=uid, account_id=data.get("account_id"), qr_token=uuid4().hex, status="pending", expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5))
            session.add(item); await session.commit(); await session.refresh(item)
            return ok(serialize_model(item), "二维码会话已创建")
        if resource == "face-verification" and subpath in {"start", "create"}:
            item = FaceVerification(owner_id=uid, account_id=data.get("account_id"), status="pending")
            session.add(item); await session.commit(); await session.refresh(item)
            return ok(serialize_model(item), "核验请求已创建")
        if resource == "payment" and subpath in {"create", "pay"}:
            item = PaymentRecord(owner_id=uid, order_id=data.get("order_id"), provider=str(data.get("provider", "manual")), amount=float(data.get("amount", 0)), status="pending")
            session.add(item); await session.commit(); await session.refresh(item)
            return ok(serialize_model(item), "支付记录已创建，等待支付渠道确认")
        if resource == "notifications" and subpath in {"send", "test"}:
            channel = str(data.get("channel", "in_app"))
            status = "sent" if channel == "in_app" else "waiting_for_configuration"
            detail = "站内通知已写入通知中心"
            if channel == "webhook":
                target = str(data.get("webhook_url") or data.get("url") or "").strip()
                if target:
                    try:
                        async with httpx.AsyncClient(timeout=8) as client:
                            response = await client.post(target, json={"title": data.get("title", "系统通知"), "content": data.get("content", ""), "source": "xianyu-rewrite"})
                        status = "sent" if response.status_code < 400 else "failed"
                        detail = f"Webhook 返回 HTTP {response.status_code}"
                    except httpx.HTTPError as exc:
                        status = "failed"; detail = str(exc)
                else:
                    detail = "Webhook 通知缺少 webhook_url"
            elif channel not in {"in_app", "webhook"}:
                detail = f"通知渠道 {channel} 尚未配置发送器"
            item = Notification(channel=channel, title=str(data.get("title", "系统通知")), content=str(data.get("content", "")), status=status)
            session.add(item); await session.commit(); await session.refresh(item)
            return ok({**serialize_model(item), "detail": detail}, "通知处理完成" if status == "sent" else "通知已记录")
        if resource == "shared-scan" and subpath in {"create", "generate"}:
            item = SharedScanSession(owner_id=uid, session_token=uuid4().hex, status="pending", expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10))
            session.add(item); await session.commit(); await session.refresh(item)
            return ok(serialize_model(item), "共享扫码会话已创建")
        if resource == "publish-capability" and subpath in {"check", "probe"}:
            item = CapabilityCheck(owner_id=uid, account_id=data.get("account_id"), capability=str(data.get("capability", "publish")), status="unavailable", detail="发布适配器尚未配置")
            session.add(item); await session.commit(); await session.refresh(item)
            return ok(serialize_model(item), "发布能力检查已记录")
        if resource in {"goofish", "distribution"} and subpath in {"import", "ingest"}:
            entries = data.get("items")
            if not isinstance(entries, list):
                raise HTTPException(422, "导入接口需要 items 数组")
            created = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if resource == "goofish":
                    item = CrawlItem(source=str(entry.get("source", "manual")), external_id=str(entry.get("external_id", uuid4().hex)), title=str(entry.get("title", "未命名商品")), price=entry.get("price"), payload=entry)
                else:
                    item = DistributionItem(owner_id=uid, source_id=str(entry.get("source_id", uuid4().hex)), title=str(entry.get("title", "未命名商品")), price=entry.get("price"), status=str(entry.get("status", "ready")), payload=entry)
                session.add(item); created.append(item)
            await session.commit()
            for item in created:
                await session.refresh(item)
            return ok({"created": len(created), "items": [serialize_model(item) for item in created]}, "数据已导入")
        if resource == "publish" and subpath in {"execute", "run"}:
            job_id = data.get("job_id")
            if job_id:
                item = (await session.execute(scope(select(PublishJob).where(PublishJob.id == int(job_id)), uid))).scalar_one_or_none()
                if item is None:
                    raise HTTPException(404, "发布任务不存在")
                item.status = "blocked"
            else:
                item = PublishJob(**_defaults(resource, {**data, "status": "blocked"}, uid))
                session.add(item)
            await session.commit(); await session.refresh(item)
            return ok({**serialize_model(item), "reason": "需要已连接账号和发布适配器"}, "发布任务已记录，等待发布能力配置")
        if resource == "external" and subpath in {"test", "check"}:
            target = str(data.get("url") or data.get("base_url") or "").strip()
            if not target:
                return ok({"status": "not_configured", "reason": "请先填写连接地址"}, "连接地址未配置")
            try:
                async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
                    response = await client.get(target)
                state = "reachable" if response.status_code < 400 else "unhealthy"
                return ok({"status": state, "http_status": response.status_code, "url": target}, "连接检查完成")
            except httpx.HTTPError as exc:
                return ok({"status": "unreachable", "url": target, "reason": str(exc)}, "连接检查失败")
        if resource in {"goofish", "listing-monitor", "distribution"} and subpath in {"crawl", "run", "sync", "execute"}:
            query = str(data.get("query", data.get("keyword", "")))
            if resource == "goofish" and subpath == "crawl":
                item = CrawlItem(source="crawl_request", external_id=uuid4().hex, title=query or "采集任务", payload={"query": query, "status": "waiting_for_connection"})
                session.add(item); await session.commit(); await session.refresh(item)
                return ok({"action": subpath, "status": "waiting_for_connection", "query": query, "record": serialize_model(item), "reason": "需要配置平台连接后执行"}, "采集任务已登记，等待连接配置")
            if resource == "distribution" and subpath == "sync":
                item = DistributionItem(owner_id=uid, source_id=str(data.get("source_id", uuid4().hex)), title=str(data.get("title", "同步任务")), price=data.get("price"), status="waiting", payload={"query": query, "status": "waiting_for_connection"})
                session.add(item); await session.commit(); await session.refresh(item)
                return ok({"action": subpath, "status": "waiting_for_connection", "record": serialize_model(item), "reason": "需要配置平台连接后执行"}, "分销任务已登记，等待连接配置")
            return ok({"action": subpath, "status": "waiting_for_connection", "query": query, "reason": "需要配置平台连接后执行"}, "任务已登记，等待连接配置")
        raise HTTPException(422, f"{label}不支持操作：{subpath}")

    return router
