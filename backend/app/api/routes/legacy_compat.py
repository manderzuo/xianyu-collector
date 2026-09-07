"""旧版功能入口的可运行兼容层。

配置型模块使用 FeatureRecord 持久化完整 payload；动作型接口会写入执行记录，
需要闲鱼账号或第三方凭据的动作会明确返回 waiting_for_connection，而不是伪造成功。
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import error, ok
from common.config import settings
from common.db.session import async_session_maker, get_session
from common.models import Account, AccountCookie, FeatureRecord, SystemSetting, User
from common.services.account_identity import extract_account_nickname
from common.services.account_renewal import renew_account_session
from common.services.goofish_mtop import parse_cookie_string
from backend.app.services.account_settings import save_account_settings
from backend.app.services.entitlements import FEATURE_ACCOUNT, ensure_quota, finalize_quota, reserve_quota

router = APIRouter(prefix="/api/v1", tags=["旧版功能兼容"])

_password_login_sessions: dict[str, dict[str, Any]] = {}
_password_login_tasks: dict[str, asyncio.Task] = {}


def _cleanup_password_login_sessions() -> None:
    cutoff = datetime.now(timezone.utc).timestamp() - 900
    for session_id, state in list(_password_login_sessions.items()):
        finished_at = float(state.get("finished_at", 0) or 0)
        if finished_at and finished_at < cutoff:
            _password_login_sessions.pop(session_id, None)


def uid(user: dict) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def is_admin(user: dict) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def serialize(item: FeatureRecord) -> dict[str, Any]:
    data = dict(item.payload or {})
    data.update({"id": item.id, "feature": item.feature, "external_id": item.external_id, "status": item.status, "note": item.note, "created_at": item.created_at, "updated_at": item.updated_at})
    return data


def _require_admin_compat(path: str, user: dict[str, Any]) -> None:
    if path.startswith("/admin/") and not is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可以访问该兼容接口")


async def records(
    feature: str,
    user: dict,
    db: AsyncSession,
    limit: int = 100,
    *,
    include_all: bool = False,
) -> list[FeatureRecord]:
    statement = select(FeatureRecord).where(FeatureRecord.feature == feature)
    if not include_all:
        statement = statement.where(FeatureRecord.owner_id == uid(user))
    return list((await db.execute(statement.order_by(FeatureRecord.id.desc()).limit(limit))).scalars().all())


async def create_record(feature: str, payload: dict[str, Any], user: dict, db: AsyncSession, *, status: str | None = None, note: str | None = None) -> FeatureRecord:
    data = dict(payload)
    item = FeatureRecord(owner_id=uid(user), feature=feature, external_id=str(data.pop("external_id", uuid4().hex)), status=status or str(data.pop("status", "active")), payload=data, note=note)
    db.add(item); await db.commit(); await db.refresh(item)
    return item


def register_crud(path: str, feature: str) -> None:
    @router.get(path)
    async def list_feature(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
        _require_admin_compat(path, user)
        include_all = path.startswith("/admin/")
        items = await records(feature, user, db, page * page_size, include_all=include_all)
        sliced = items[(page - 1) * page_size: page * page_size]
        total_statement = select(func.count()).select_from(FeatureRecord).where(FeatureRecord.feature == feature)
        if not include_all: total_statement = total_statement.where(FeatureRecord.owner_id == uid(user))
        total = int((await db.execute(total_statement)).scalar_one())
        return ok({"items": [serialize(item) for item in sliced], "total": total, "page": page, "page_size": page_size}, "查询成功")

    @router.post(path)
    async def create_feature(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
        _require_admin_compat(path, user)
        item = await create_record(feature, payload or {}, user, db)
        return ok(serialize(item), "记录已创建")

    @router.get(f"{path}/{{record_id}}")
    async def get_feature(record_id: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
        _require_admin_compat(path, user)
        # 旧版不少列表接口挂在资源名下，例如 /cards/selectable、
        # /chat-new/accounts。不能让字符串被 FastAPI 强转 int 后直接返回 422。
        if not record_id.isdigit():
            items = await records(feature, user, db, include_all=path.startswith("/admin/"))
            values = [serialize(item) for item in items]
            return ok({"items": values, "list": values, "total": len(values), "path": f"{path}/{record_id}"}, "查询成功")
        record_id = int(record_id)
        statement = select(FeatureRecord).where(FeatureRecord.id == record_id, FeatureRecord.feature == feature)
        if not path.startswith("/admin/"): statement = statement.where(FeatureRecord.owner_id == uid(user))
        item = (await db.execute(statement)).scalar_one_or_none()
        if item is None: raise HTTPException(404, "记录不存在")
        return ok(serialize(item), "查询成功")

    @router.put(f"{path}/{{record_id}}")
    async def update_feature(record_id: int, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
        _require_admin_compat(path, user)
        statement = select(FeatureRecord).where(FeatureRecord.id == record_id, FeatureRecord.feature == feature)
        if not path.startswith("/admin/"): statement = statement.where(FeatureRecord.owner_id == uid(user))
        item = (await db.execute(statement)).scalar_one_or_none()
        if item is None: raise HTTPException(404, "记录不存在")
        data = dict(payload or {})
        if "status" in data: item.status = str(data.pop("status"))
        if "note" in data: item.note = str(data.pop("note"))
        data.pop("id", None); data.pop("owner_id", None); data.pop("feature", None)
        item.payload = {**(item.payload or {}), **data}
        await db.commit(); await db.refresh(item)
        return ok(serialize(item), "记录已更新")

    @router.delete(f"{path}/{{record_id}}")
    async def delete_feature(record_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
        _require_admin_compat(path, user)
        statement = select(FeatureRecord).where(FeatureRecord.id == record_id, FeatureRecord.feature == feature)
        if not path.startswith("/admin/"): statement = statement.where(FeatureRecord.owner_id == uid(user))
        item = (await db.execute(statement)).scalar_one_or_none()
        if item is None: raise HTTPException(404, "记录不存在")
        await db.delete(item); await db.commit()
        return ok({"id": record_id, "deleted": True}, "记录已删除")


for _path, _feature in {
    "/blacklist/personal": "blacklist", "/personal-settings": "personal-settings",
    "/user-settings": "user-settings", "/notification-channels": "notification-channels",
    "/message-notifications": "message-notifications", "/product-monitor/categories": "monitor-categories",
    "/product-monitor/order-fallback-accounts": "order-fallback-accounts",
    "/product-monitor/collect-fallback-accounts": "collect-fallback-accounts",
    "/product-monitor/logs": "monitor-logs", "/product-monitor/items": "monitor-items",
    "/personal-addresses": "personal-addresses", "/card-dock": "card-dock",
    "/advertisements": "advertisements", "/feedbacks": "feedbacks",
    "/popup-announcements": "popup-announcements", "/external/account-cookie": "external-account-cookie",
    "/external/enabled-accounts": "external-enabled-accounts", "/external/category": "external-category",
    "/external/publish": "external-publish", "/product-publish/materials": "product-materials",
    "/product-publish/addresses": "product-addresses", "/confirm-receipt-messages": "confirm-receipt-messages",
    "/auto-reply-logs": "auto-reply-logs", "/account-login-logs": "account-login-logs",
    "/db-backup-logs": "db-backup-logs", "/risk-control-logs": "risk-control-logs",
    "/admin/logs": "admin-logs", "/admin/redelivery-batches": "admin-redelivery-batches",
    "/admin/account-login-logs": "admin-account-login-logs", "/admin/rate-batches": "admin-rate-batches",
    "/admin/polish-batches": "admin-polish-batches", "/admin/login-renew-batches": "admin-login-renew-batches",
    "/admin/token-renewal-batches": "admin-token-renewal-batches", "/admin/cookies-refresh-batches": "admin-cookies-refresh-batches",
    "/admin/api-cookie-renew-batches": "admin-api-cookie-renew-batches", "/admin/db-backup-logs": "admin-db-backup-logs",
    "/admin/ad-manage": "admin-ad-manage", "/admin/fund-flows": "admin-fund-flows",
    "/ai-reply-settings": "ai-reply-settings",
    "/chat-new": "chat-new", "/admin/scheduled-tasks": "admin-scheduled-tasks",
    "/admin/announcements": "admin-announcements", "/cookie-refresh": "cookie-refresh",
}.items():
    register_crud(_path, _feature)


@router.get("/product-publish/logs")
async def publish_logs(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    items = await records("product-publish-jobs", user, db)
    return ok({"items": [serialize(item) for item in items], "total": len(items)}, "查询成功")


async def _run_password_login(
    session_id: str,
    owner_id: int,
    account_key: str,
    username: str,
    password: str,
    show_browser: bool,
) -> None:
    state = _password_login_sessions.get(session_id)
    if state is None or state.get("status") == "cancelled":
        return
    try:
        cookie_value = ""
        existing_id: int | None = int(account_key) if account_key.isdigit() else None
        if existing_id is not None:
            async with async_session_maker() as session:
                account = (
                    await session.execute(
                        select(Account).where(Account.id == existing_id, Account.user_id == owner_id)
                    )
                ).scalar_one_or_none()
                if account is not None:
                    cookie_value = str(account.cookie or "").strip()

        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=20, pool=30)) as client:
            response = await client.post(
                f"{settings.websocket_service_url.rstrip('/')}/internal/cookies/password-renew",
                json={
                    "account_id": account_key,
                    "cookies_str": cookie_value,
                    "username": username,
                    "password": password,
                    "show_browser": bool(show_browser),
                },
                headers={"X-Internal-Token": settings.jwt_secret},
            )
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else {}
        data = data if isinstance(data, dict) else {}
        if not response.is_success or not payload.get("success"):
            status = str(data.get("status") or payload.get("code") or "failed")
            state.update({
                "status": "verification_required" if status == "verification_required" else "failed",
                "message": str(payload.get("message") or data.get("message") or "密码登录失败")[:500],
                "error": str(payload.get("message") or data.get("message") or "密码登录失败")[:500],
            })
            return

        new_cookie = str(data.get("new_cookies_str") or "").strip()
        cookie_map = parse_cookie_string(new_cookie)
        if not new_cookie or not cookie_map.get("unb"):
            state.update({"status": "failed", "message": "密码登录成功但未返回有效 Cookie", "error": "未返回有效 Cookie"})
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        unb = str(cookie_map.get("unb") or "")[:64] or None
        nickname = extract_account_nickname(new_cookie)
        async with async_session_maker() as session:
            account = None
            if existing_id is not None:
                account = (
                    await session.execute(
                        select(Account).where(Account.id == existing_id, Account.user_id == owner_id)
                    )
                ).scalar_one_or_none()
            if account is None and unb:
                account = (
                    await session.execute(
                        select(Account).where(Account.goofish_id == unb, Account.user_id == owner_id)
                    )
                ).scalar_one_or_none()
            is_new = account is None
            reservation = None
            if account is None:
                owner = await session.get(User, owner_id)
                principal = {"sub": str(owner_id), "role": owner.role if owner is not None else "user", "plan_code": owner.plan_code if owner is not None else "NORMAL"}
                reservation = await reserve_quota(
                    session,
                    principal,
                    FEATURE_ACCOUNT,
                    resource_key=f"password-login:{session_id}",
                    idempotency_key=f"account:password-login:{session_id}",
                )
                account = Account(
                    user_id=owner_id,
                    account_name=nickname or username[:64],
                    goofish_id=unb,
                    cookie=new_cookie,
                    status="active",
                    cookie_expire_at=now + timedelta(days=30),
                )
                session.add(account)
                await session.flush()
            else:
                account.cookie = new_cookie
                account.status = "active"
                account.cookie_expire_at = now + timedelta(days=30)
                if nickname and (not account.account_name or account.account_name.isdigit()):
                    account.account_name = nickname
            session.add(AccountCookie(account_id=account.id, cookie_value=new_cookie, status="active", expires_at=now))
            finalize_quota(reservation)
            await save_account_settings(
                session,
                owner_id,
                int(account.id),
                {"username": username, "login_password": password, "show_browser": bool(show_browser)},
            )
            # save_account_settings 已提交事务；重新刷新账号对象后再通知运行时。
            account_id = int(account.id)

        runtime = None
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                runtime_response = await client.post(
                    f"{settings.websocket_service_url.rstrip('/')}/internal/accounts/{account_id}/restart",
                    json={"cookie_value": new_cookie, "user_id": owner_id},
                    headers={"X-Internal-Token": settings.jwt_secret},
                )
            runtime = runtime_response.json().get("data")
        except (httpx.HTTPError, ValueError):
            runtime = {"status": "unavailable"}
        state.update({
            "status": "success",
            "message": "密码登录成功",
            "account_id": str(account_id),
            "is_new_account": is_new,
            "cookie_count": len(cookie_map),
            "runtime": runtime,
        })
    except asyncio.CancelledError:
        state.update({"status": "cancelled", "message": "密码登录已取消"})
        raise
    except Exception as exc:
        state.update({"status": "failed", "message": f"密码登录异常：{str(exc)[:500]}", "error": f"密码登录异常：{str(exc)[:500]}"})
    finally:
        _password_login_tasks.pop(session_id, None)
        state["finished_at"] = datetime.now(timezone.utc).timestamp()


@router.post("/password-login")
async def password_login(payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    _cleanup_password_login_sessions()
    data = payload or {}
    account_key = str(data.get("account_id") or data.get("account") or "").strip()
    username = str(data.get("account") or data.get("username") or account_key).strip()
    password = str(data.get("password") or "")
    if not account_key or not username or not password:
        return error("请输入账号和密码", code="invalid_request", data={"status": "failed"})
    # 密码登录同样可能创建新闲鱼账号，配额耗尽时在启动外部登录任务前直接拒绝。
    await ensure_quota(db, user, FEATURE_ACCOUNT)
    session_id = f"pwd-{uuid4().hex}"
    _password_login_sessions[session_id] = {"status": "processing", "message": "密码登录任务已启动", "owner_id": uid(user)}
    task = asyncio.create_task(
        _run_password_login(
            session_id,
            uid(user),
            account_key,
            username,
            password,
            bool(data.get("show_browser", False)),
        )
    )
    _password_login_tasks[session_id] = task
    return {"success": True, "session_id": session_id, "status": "processing", "message": "登录任务已启动，请等待"}


@router.delete("/password-login/cancel/{session_id}")
async def cancel_password_login(session_id: str, user=Depends(get_current_user)):
    state = _password_login_sessions.get(session_id)
    if state is None or int(state.get("owner_id", 0) or 0) != uid(user):
        return ok({"session_id": session_id, "status": "not_found"}, "登录会话不存在")
    task = _password_login_tasks.get(session_id)
    if task and not task.done():
        task.cancel()
    state.update({"status": "cancelled", "message": "密码登录会话已取消"})
    return {"success": True, "session_id": session_id, "status": "cancelled", "message": "密码登录会话已取消"}


@router.get("/password-login/check/{session_id}")
async def check_password_login(session_id: str, user=Depends(get_current_user)):
    state = _password_login_sessions.get(session_id)
    if state is None or int(state.get("owner_id", 0) or 0) != uid(user):
        return {"status": "not_found", "message": "登录会话不存在", "error": "登录会话不存在"}
    return {**state, "session_id": session_id}


@router.post("/cookie-refresh/{action}/{account_id}")
async def cookie_refresh_action(
    action: str,
    account_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """兼容旧版单账号 Cookie 续期按钮，并执行真实续期。"""
    if action not in {"renew", "refresh", "login", "renew-login", "start", "trigger"}:
        raise HTTPException(status_code=404, detail="不支持的 Cookie 续期动作")
    statement = select(Account).where(Account.id == account_id, Account.user_id == uid(user))
    account = (await db.execute(statement)).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    result = await renew_account_session(
        db,
        account,
        source="manual",
        force=True,
        notify_runtime=True,
    )
    if result.get("success"):
        return ok(result, "Cookie续期成功")
    return error(
        str(result.get("message") or "Cookie续期失败"),
        code="manual_login_required" if result.get("needs_manual_login") else "cookie_renew_failed",
        data=result,
    )


@router.post("/data-analysis/{action}")
async def analysis_action(action: str, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = await create_record("analysis-jobs", {"action": action, **(payload or {})}, user, db, status="completed", note="基于当前数据库汇总")
    return ok(serialize(item), "分析任务已完成")


def _local_version() -> str:
    for candidate in (Path("/app/VERSION.txt"), Path(__file__).resolve().parents[4] / "VERSION.txt"):
        try:
            value = candidate.read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            continue
    return os.getenv("APP_VERSION", "1.0.3").strip() or "1.0.3"


def _version_parts(value: str) -> tuple[int, ...]:
    parts = tuple(int(item) for item in re.findall(r"\d+", value))
    return parts or (0,)


@router.get("/version/current")
@router.get("/version/check")
async def version_info():
    current = _local_version()
    payload: dict[str, Any] = {
        "version": current,
        "current_version": current,
        "remote_version": current,
        "has_update": False,
        "update_available": False,
        "description": "",
        "filename": "",
        "download_url": "",
        "source": "local",
    }
    manifest_url = settings.update_manifest_url.strip()
    if not manifest_url:
        return ok(payload, "版本信息来自本地构建")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=2.5, read=5, write=5, pool=5)) as client:
            response = await client.get(
                manifest_url,
                headers={"Cache-Control": "no-cache"},
                params={"_client_check": int(datetime.now(timezone.utc).timestamp())},
            )
        response.raise_for_status()
        manifest = response.json()
        remote = str(manifest.get("version") or "").strip()
        if remote:
            has_update = _version_parts(remote) > _version_parts(current)
            payload.update({
                "remote_version": remote,
                "has_update": has_update,
                "update_available": has_update,
                "description": str(manifest.get("notes") or "").strip(),
                "filename": str(manifest.get("image_tag") or "").strip(),
                "download_url": manifest_url,
                "source": "tencent-release-manifest",
            })
        return ok(payload, "版本检查完成")
    except (httpx.HTTPError, ValueError, TypeError):
        return ok(payload, "暂时无法连接更新服务器，当前版本仍可正常使用")


async def _probe_service(url: str) -> bool:
    """Probe a service health endpoint without exposing internal addresses."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=1.5, read=2.5, write=2.5, pool=2.5)) as client:
            response = await client.get(f"{url.rstrip('/')}/health")
        return response.is_success
    except httpx.HTTPError:
        return False


@router.get("/system-control/status")
async def system_status(user=Depends(get_current_user)):
    """Return the actual health of all managed services.

    The old response used a mapping of hard-coded ``running`` strings while
    the frontend consumes an array of ``{key, online}`` objects.  Keep the
    endpoint and keys stable, but derive the values from health probes.
    """
    service_specs = (
        ("websocket", "消息服务", settings.websocket_service_url, settings.websocket_port),
        ("backend-web", "后端服务", settings.backend_service_url, settings.backend_web_port),
        ("scheduler", "定时任务服务", settings.scheduler_service_url, settings.scheduler_port),
    )
    online_values = await asyncio.gather(*(_probe_service(url) for _, _, url, _ in service_specs))
    services = [
        {"key": key, "label": label, "port": port, "online": online}
        for (key, label, _, port), online in zip(service_specs, online_values)
    ]
    return ok({"runtime": settings.environment, "services": services}, "查询成功")


@router.post("/system-control/restart/{service_key}")
async def restart_service(service_key: str, user=Depends(get_current_user)):
    if not is_admin(user): raise HTTPException(403, "仅管理员可以操作服务")
    if service_key not in {"backend-web", "websocket", "scheduler"}:
        raise HTTPException(404, "服务不存在")

    # Compose 部署中 backend 容器通常没有 Docker Socket，不能从 API 控制
    # 宿主机重启容器。调度器提供进程内 reload，可重新读取任务开关/间隔并
    # 重建 APScheduler 任务，不需要提升容器权限，也不会影响数据库和消息服务。
    if service_key == "scheduler":
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(f"{settings.scheduler_service_url.rstrip('/')}/internal/reload", headers={"X-Internal-Token": settings.jwt_secret})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return error(
                f"定时任务服务重新加载请求失败：{str(exc)[:300]}",
                code="scheduler_reload_failed",
                data={"service": service_key, "status": "unavailable"},
            )
        if not payload.get("success"):
            return error(
                str(payload.get("message") or "定时任务服务重新加载失败"),
                code=str(payload.get("code") or "scheduler_reload_failed"),
                data=payload.get("data"),
            )
        return ok(
            payload.get("data") or {"service": service_key, "status": "running", "mode": "in_process_reload"},
            payload.get("message") or "定时任务服务已重新加载",
        )

    return error(
        "当前部署环境不支持由 API 重启服务，请通过 Docker Compose 或部署编排执行",
        code="restart_not_supported",
        data={"service": service_key, "status": "not_supported_in_container"},
    )


@router.get("/health/ping")
async def health_ping():
    return ok({"status": "running", "service": "backend-web"}, "服务正常")


@router.get("/system-settings/public")
async def public_settings(db: AsyncSession = Depends(get_session)):
    """登录前可读取的非敏感设置。"""
    public_keys = {
        "registration_enabled", "login_captcha_enabled",
        "login.system_name", "login.system_title", "login.system_description",
        "auth.footer_ad_html", "disclaimer.title", "disclaimer.content",
        "disclaimer.checkbox_text", "disclaimer.agree_button_text", "disclaimer.disagree_button_text",
    }
    rows = (await db.execute(select(SystemSetting).where(SystemSetting.setting_key.in_(public_keys)))).scalars().all()
    values = {row.setting_key: row.setting_value for row in rows}
    values.setdefault("registration_enabled", "true")
    values.setdefault("login.system_name", settings.brand_name)
    values.setdefault("brand_name", settings.brand_name)
    values.setdefault("brand_domain", settings.brand_domain)
    # 未配置极验服务时不能默认开启登录滑块，否则登录页会被一个
    # 无法初始化的验证码组件阻断。管理员仍可在系统设置中显式开启。
    values.setdefault("login_captcha_enabled", False)
    values.setdefault("features", {"qr_login": True})
    return ok(values, "查询成功")
