# -*- coding: utf-8 -*-
"""WebSocket 长连接服务。"""
import logging
import secrets
import shutil
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from common.config import settings
from common.db.session import async_session_maker, init_db
from common.models.accounts import Account
from websocket.app.connection import ConnectionManager
from websocket.app.handler import handle_socket
from websocket.app.xianyu_runtime import runtime_manager
from websocket.app.browser_cookie_renew import renew_browser_cookies, renew_password_cookies

app = FastAPI(title=f"{settings.brand_name} WebSocket", version="1.0.5")
manager = ConnectionManager()
logger = logging.getLogger("xr.websocket")


@app.middleware("http")
async def protect_internal_routes(request, call_next):
    """所有服务间 HTTP 接口必须携带部署级内部令牌。"""
    if request.url.path.startswith("/internal/"):
        token = request.headers.get("X-Internal-Token", "")
        if not token or not secrets.compare_digest(token, settings.jwt_secret):
            return JSONResponse(status_code=401, content={"detail": "内部调用凭证无效"})
    return await call_next(request)


class AccountRuntimeRequest(BaseModel):
    cookie_value: str = ""
    user_id: int | None = None


class ChatQueryRequest(BaseModel):
    cursor: int | None = None
    limit: int = 20


class SendTextRequest(BaseModel):
    cid: str
    to_user_id: str
    text: str


class SendToBuyerRequest(BaseModel):
    buyer_id: str
    text: str


class CreateChatRequest(BaseModel):
    to_user_id: str
    item_id: str


class SendImageRequest(BaseModel):
    cid: str
    to_user_id: str
    image_path: str


class SendImageUrlRequest(BaseModel):
    cid: str
    to_user_id: str
    image_url: str


class RecallMessageRequest(BaseModel):
    message_id: str


class BrowserCookieRenewRequest(BaseModel):
    account_id: str
    cookies_str: str = ""


class PasswordCookieRenewRequest(BaseModel):
    account_id: str
    cookies_str: str = ""
    username: str
    password: str
    show_browser: bool = False


class BrowserDataCleanupRequest(BaseModel):
    account_ids: list[str] = []


@app.on_event("startup")
async def startup() -> None:
    try:
        await init_db()
        logger.info("database schema initialized")
    except Exception as exc:  # pragma: no cover - 依赖外部 MySQL
        logger.warning("database initialization skipped: %s", exc)
    await runtime_manager.start_enabled_accounts()


@app.get("/health", tags=["系统"])
async def health():
    stats = await runtime_manager.connection_stats()
    return {"success": True, "code": "ok", "message": "操作成功", "data": {"service": "websocket", "status": "running", "connections": stats["connected"], "browser_connections": manager.size, "account_connections": stats}}


@app.post("/internal/accounts/{account_id}/{action}", tags=["内部账号运行时"])
async def account_runtime(account_id: str, action: str, payload: AccountRuntimeRequest | None = None):
    """启动、重启或停止一个闲鱼账号的真实长连接。"""
    if action not in {"start", "restart", "stop"}:
        raise HTTPException(status_code=404, detail="不支持的账号运行时动作")
    try:
        numeric_account_id = int(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="账号ID无效") from exc

    payload = payload or AccountRuntimeRequest()
    if action == "stop":
        await runtime_manager.stop(str(numeric_account_id))
        return {"success": True, "code": "ok", "message": "账号连接已停止", "data": await runtime_manager.get_status(str(numeric_account_id))}

    cookie_value = payload.cookie_value.strip()
    user_id = payload.user_id
    if not cookie_value:
        async with async_session_maker() as session:
            account = (
                await session.execute(select(Account).where(Account.id == numeric_account_id))
            ).scalar_one_or_none()
        if account is None:
            raise HTTPException(status_code=404, detail="账号不存在")
        cookie_value = str(account.cookie or "").strip()
        user_id = int(account.user_id)
    if not cookie_value:
        raise HTTPException(status_code=422, detail="账号没有可用Cookie，请重新扫码登录")

    runtime = await runtime_manager.start(str(numeric_account_id), cookie_value, user_id)
    return {"success": True, "code": "ok", "message": "账号连接任务已启动", "data": runtime}


@app.get("/internal/accounts/{account_id}/status", tags=["内部账号运行时"])
async def account_runtime_status(account_id: str):
    return {"success": True, "code": "ok", "message": "查询成功", "data": await runtime_manager.get_status(str(account_id))}


@app.get("/internal/accounts/connection-stats", tags=["内部账号运行时"])
async def account_connection_stats():
    """返回真正已完成闲鱼 WebSocket 握手的账号集合。"""
    return {"success": True, "code": "ok", "message": "查询成功", "data": await runtime_manager.connection_stats()}


@app.post("/internal/browser-data/cleanup", tags=["内部账号运行时"])
async def cleanup_browser_data(
    payload: BrowserDataCleanupRequest,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    """清理调度服务筛出的禁用账号浏览器 profile。"""
    if not x_internal_token or x_internal_token != settings.jwt_secret:
        raise HTTPException(status_code=401, detail="内部调用凭证无效")
    root = Path(settings.browser_data_dir).resolve()
    cleaned_count = skipped_count = failed_count = 0
    total_bytes = 0
    results: list[dict[str, Any]] = []
    for raw_id in payload.account_ids:
        account_id = str(raw_id or "").strip()
        if not account_id.isdigit():
            failed_count += 1
            results.append({"account_id": account_id, "status": "failed", "message": "账号ID无效"})
            continue
        directory = (root / f"user_{account_id}").resolve()
        if directory.parent != root or directory.name != f"user_{account_id}":
            failed_count += 1
            results.append({"account_id": account_id, "status": "failed", "message": "浏览器目录路径无效"})
            continue
        if not directory.is_dir():
            skipped_count += 1
            results.append({"account_id": account_id, "status": "skipped", "message": "浏览器目录不存在"})
            continue
        try:
            size = sum(item.stat().st_size for item in directory.rglob("*") if item.is_file())
            shutil.rmtree(directory)
            cleaned_count += 1
            total_bytes += size
            results.append({"account_id": account_id, "status": "success", "bytes": size})
        except OSError as exc:
            failed_count += 1
            results.append({"account_id": account_id, "status": "failed", "message": str(exc)[:500]})
    return {"success": failed_count == 0, "code": "ok" if failed_count == 0 else "partial_failure", "message": "浏览器数据清理完成" if failed_count == 0 else "部分浏览器数据清理失败", "data": {"cleaned_count": cleaned_count, "skipped_count": skipped_count, "failed_count": failed_count, "total_bytes": total_bytes, "results": results}}


@app.post("/internal/cookies/browser-renew", tags=["内部账号运行时"])
async def browser_cookie_renew(
    payload: BrowserCookieRenewRequest,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    """由 scheduler/backend 委托执行持久化浏览器 Cookie 续期。"""
    cookie_value = payload.cookies_str.strip()
    try:
        numeric_account_id = int(payload.account_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="账号ID无效") from exc
    if not cookie_value:
        async with async_session_maker() as session:
            account = (
                await session.execute(select(Account).where(Account.id == numeric_account_id))
            ).scalar_one_or_none()
        if account is None:
            raise HTTPException(status_code=404, detail="账号不存在")
        cookie_value = str(account.cookie or "").strip()
    if not cookie_value:
        raise HTTPException(status_code=422, detail="账号没有可用 Cookie")
    result = await renew_browser_cookies(cookie_value, payload.account_id)
    return {
        "success": bool(result.get("success")),
        "code": "ok" if result.get("success") else "browser_renew_failed",
        "message": result.get("message") or "浏览器续期完成",
        "data": result,
    }


@app.post("/internal/cookies/password-renew", tags=["内部账号运行时"])
async def password_cookie_renew(
    payload: PasswordCookieRenewRequest,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    """由后台续期服务委托执行账号密码登录兜底。"""
    account_key = str(payload.account_id or "").strip()
    if not account_key:
        raise HTTPException(status_code=422, detail="账号标识不能为空")
    if not payload.username.strip() or not payload.password:
        raise HTTPException(status_code=422, detail="账号或密码不能为空")
    cookie_value = payload.cookies_str.strip()
    if not cookie_value:
        if account_key.isdigit():
            async with async_session_maker() as session:
                account = (
                    await session.execute(select(Account).where(Account.id == int(account_key)))
                ).scalar_one_or_none()
            if account is not None:
                cookie_value = str(account.cookie or "").strip()
    result = await renew_password_cookies(
        cookie_value,
        account_key,
        payload.username,
        payload.password,
        payload.show_browser,
    )
    return {
        "success": bool(result.get("success")),
        "code": "ok" if result.get("success") else str(result.get("status") or "password_login_failed"),
        "message": result.get("message") or "密码登录完成",
        "data": result,
    }


@app.post("/internal/chat/{account_id}/conversations", tags=["内部账号运行时"])
async def account_conversations(account_id: str, payload: ChatQueryRequest = Body(default_factory=ChatQueryRequest)):
    """通过已建立的闲鱼 IM 长连接读取真实会话列表。"""
    cursor = payload.cursor if payload.cursor is not None else 9007199254740991
    result = await runtime_manager.request(account_id, "/r/Conversation/listNewestPagination", [cursor, max(1, min(payload.limit, 100))])
    return {"success": True, "code": "ok", "message": "查询成功", "data": result.get("body") or {}}


@app.post("/internal/chat/{account_id}/messages", tags=["内部账号运行时"])
async def account_messages(account_id: str, payload: dict[str, Any] = Body(...)):
    # 保留 JSON 兼容性：消息接口还需要 cid，不能只用分页模型。
    data = dict(payload)
    cid = str(data.get("cid") or "").strip()
    if not cid:
        raise HTTPException(422, "会话ID不能为空")
    cursor = data.get("cursor") if data.get("cursor") is not None else 9007199254740991
    result = await runtime_manager.request(account_id, "/r/MessageManager/listUserMessages", [cid if "@goofish" in cid else f"{cid}@goofish", False, cursor, max(1, min(int(data.get("limit") or 20), 100)), False])
    return {"success": True, "code": "ok", "message": "查询成功", "data": result.get("body") or {}}


@app.post("/internal/chat/{account_id}/send-text", tags=["内部账号运行时"])
async def account_send_text(account_id: str, payload: SendTextRequest):
    if not payload.text.strip():
        raise HTTPException(422, "消息内容不能为空")
    result = await runtime_manager.send_text(account_id, payload.cid, payload.to_user_id, payload.text.strip())
    return {"success": True, "code": "ok", "message": "发送成功", "data": result}


@app.post("/internal/chat/{account_id}/create-chat", tags=["内部账号运行时"])
async def account_create_chat(account_id: str, payload: CreateChatRequest):
    if not payload.to_user_id.strip():
        raise HTTPException(422, "对方用户ID不能为空")
    if not payload.item_id.strip():
        raise HTTPException(422, "商品ID不能为空")
    result = await runtime_manager.create_chat(account_id, payload.to_user_id.strip(), payload.item_id.strip())
    return {"success": True, "code": "ok", "message": "会话创建成功", "data": result}


@app.post("/internal/chat/{account_id}/send-to-buyer", tags=["内部账号运行时"])
async def account_send_to_buyer(account_id: str, payload: SendToBuyerRequest):
    if not payload.buyer_id.strip():
        raise HTTPException(422, "买家ID不能为空")
    if not payload.text.strip():
        raise HTTPException(422, "消息内容不能为空")
    try:
        result = await runtime_manager.send_text_to_buyer(account_id, payload.buyer_id.strip(), payload.text.strip())
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"success": True, "code": "ok", "message": "发送成功", "data": result}


@app.post("/internal/chat/{account_id}/send-image", tags=["内部账号运行时"])
async def account_send_image(account_id: str, payload: SendImageRequest):
    from pathlib import Path

    root = Path(settings.static_dir).resolve()
    image_path = Path(payload.image_path).resolve()
    if root not in image_path.parents:
        raise HTTPException(status_code=400, detail="图片路径不在共享静态目录内")
    result = await runtime_manager.send_image(account_id, payload.cid, payload.to_user_id, str(image_path))
    return {"success": True, "code": "ok", "message": "图片发送成功", "data": result}


@app.post("/internal/chat/{account_id}/send-image-url", tags=["内部账号运行时"])
async def account_send_image_url(account_id: str, payload: SendImageUrlRequest):
    if not payload.image_url.strip():
        raise HTTPException(status_code=422, detail="图片地址不能为空")
    result = await runtime_manager.send_image_url(account_id, payload.cid, payload.to_user_id, payload.image_url.strip())
    return {"success": True, "code": "ok", "message": "图片发送成功", "data": result}


@app.post("/internal/chat/{account_id}/recall-message", tags=["内部账号运行时"])
async def account_recall_message(account_id: str, payload: RecallMessageRequest):
    result = await runtime_manager.recall_message(account_id, payload.message_id)
    return {"success": True, "code": "ok", "message": "消息已撤回", "data": result}


@app.websocket("/api/v1/ws")
async def websocket_endpoint(socket: WebSocket):
    await handle_socket(socket, manager)
