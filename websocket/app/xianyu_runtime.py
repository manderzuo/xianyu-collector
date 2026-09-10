# -*- coding: utf-8 -*-
"""闲鱼账号长连接运行时。

这里实现的是账号管理真正依赖的最小运行闭环：读取 Cookie、获取 IM Token、
建立闲鱼 WebSocket、完成注册与心跳，并把真实连接状态提供给 backend。
消息业务可以在此连接之上继续扩展，但不能把“任务已启动”当成“账号在线”。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import websockets
from sqlalchemy import select

from common.config import settings
from common.db.session import async_session_maker
from common.models.accounts import Account
from common.models.system import SystemSetting
from common.services.account_renewal import renew_account_session
from common.services.cookie_renewal import is_session_expired_message
from common.services.remote_token_api import request_remote_xianyu_token
from common.services.goofish_mtop import parse_cookie_string, serialize_cookies
from common.utils.xianyu_push import extract_events

logger = logging.getLogger("xr.websocket.runtime")

IM_TOKEN_API = "mtop.taobao.idlemessage.pc.login.token"
IM_APP_KEY = "34839810"
IM_DEVICE_APP_KEY = "444e9908a51d1cb236a27862abc769c9"
GOOFISH_WS_URL = "wss://wss-goofish.dingtalk.com/"
WS_APP_KEY = "444e9908a51d1cb236a27862abc769c9"


def _parse_cookie(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in str(value or "").split(";"):
        key, separator, item = part.strip().partition("=")
        if separator and key:
            result[key] = item
    return result


def _generate_device_id(user_id: str) -> str:
    # 与源程序的设备 ID 结构保持一致：UUID + 闲鱼用户标识。
    return f"{uuid.uuid4()}-{str(user_id or 'unknown').strip()}"


def _generate_mid() -> str:
    return f"{random.randrange(1000)}{int(time.time() * 1000)} 0"


def _generate_uuid() -> str:
    return str(uuid.uuid4())


def _sign(timestamp: str, token: str, data: str) -> str:
    return hashlib.md5(f"{token}&{timestamp}&{IM_APP_KEY}&{data}".encode()).hexdigest()


def _headers(cookie_value: str) -> dict[str, str]:
    return {
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "cache-control": "no-cache",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.goofish.com",
        "pragma": "no-cache",
        "referer": "https://www.goofish.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/139.0.0.0 Safari/537.36",
        "cookie": str(cookie_value or "").replace("\n", "").replace("\r", ""),
    }


async def _load_token_settings() -> tuple[str, str, str]:
    keys = {"token.api_mode", "token.remote_url", "token.remote_secret_key"}
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                select(SystemSetting.setting_key, SystemSetting.setting_value).where(
                    SystemSetting.setting_key.in_(keys)
                )
            )
        ).all()
    values = {str(key): str(value or "").strip() for key, value in rows}
    mode = values.get("token.api_mode", "web").lower()
    return mode if mode in {"web", "remote"} else "web", values.get("token.remote_url", ""), values.get("token.remote_secret_key", "")


async def _request_web_token(cookie_value: str, device_id: str) -> tuple[str, str, str]:
    current_cookie = str(cookie_value or "").strip()
    cookies = _parse_cookie(current_cookie)
    if not cookies:
        raise RuntimeError("账号 Cookie 为空")
    user_id = cookies.get("unb") or cookies.get("munb")
    if not user_id:
        raise RuntimeError("Cookie 缺少 unb，无法生成闲鱼设备标识")
    data_value = json.dumps(
        {"appKey": IM_DEVICE_APP_KEY, "deviceId": device_id},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    api_url = f"{settings.goofish_mtop_host}/h5/{IM_TOKEN_API}/1.0/"
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15, read=30, write=15, pool=30),
        follow_redirects=True,
        proxy=settings.goofish_proxy or None,
    ) as client:
        # Token 过期时，mtop 会先下发新的 _m_h5_tk；合并后重签并重试一次。
        # 旧版在这里会保存响应 Cookie，若不处理就会一直用旧 token 重试。
        last_detail = "闲鱼 Token 接口未返回有效 Token"
        for attempt in range(2):
            cookies = _parse_cookie(current_cookie)
            timestamp = str(int(time.time() * 1000))
            token_cookie = cookies.get("_m_h5_tk") or cookies.get("m_h5_tk") or ""
            signing_token = token_cookie.split("_", 1)[0] if token_cookie else ""
            params = {
                "jsv": "2.7.2",
                "appKey": IM_APP_KEY,
                "t": timestamp,
                "sign": _sign(timestamp, signing_token, data_value),
                "v": "1.0",
                "type": "originaljson",
                "accountSite": "xianyu",
                "dataType": "json",
                "timeout": "20000",
                "api": IM_TOKEN_API,
                "sessionOption": "AutoLoginOnly",
                "dangerouslySetWindvaneParams": "%5Bobject%20Object%5D",
                "smToken": "token",
                "queryToken": "sm",
                "sm": "sm",
                "spm_cnt": "a21ybx.im.0.0",
            }
            response = await client.post(
                api_url,
                params=params,
                data={"data": data_value},
                headers=_headers(current_cookie),
                cookies=cookies,
            )
            response.raise_for_status()
            response_cookies: dict[str, str] = {}
            for header in response.headers.get_list("set-cookie"):
                # 闲鱼会带上 Partitioned 等新版属性，Python SimpleCookie
                # 在部分版本中会因此整行解析失败；这里只需取 Set-Cookie
                # 的第一个 name=value 对即可。
                pair = str(header).split(";", 1)[0].strip()
                name, separator, value = pair.partition("=")
                if separator and name.strip() and value.strip():
                    response_cookies[name.strip()] = value.strip()
            if response_cookies:
                merged = parse_cookie_string(current_cookie)
                merged.update(response_cookies)
                current_cookie = serialize_cookies(merged)
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("闲鱼 Token 接口返回内容不是 JSON") from exc
            ret = payload.get("ret") or []
            data = payload.get("data") or {}
            access_token = data.get("accessToken") if isinstance(data, dict) else None
            if isinstance(access_token, str) and access_token.strip() and any("SUCCESS" in str(item) for item in ret):
                return access_token.strip(), device_id, current_cookie
            last_detail = str(ret[0] if ret else last_detail)
            if attempt == 0 and response_cookies:
                continue
            break
    raise RuntimeError(last_detail[:500])


async def _request_token(cookie_value: str, user_id: int | None) -> tuple[str, str, str, str]:
    cookies = _parse_cookie(cookie_value)
    platform_user_id = cookies.get("unb") or cookies.get("munb") or str(user_id or "unknown")
    device_id = _generate_device_id(platform_user_id)
    mode, remote_url, remote_secret = await _load_token_settings()
    local_error: Exception | None = None
    try:
        token, resolved_device_id, refreshed_cookie = await _request_web_token(cookie_value, device_id)
        return token, resolved_device_id, "web", refreshed_cookie
    except Exception as exc:
        local_error = exc
        logger.warning("账号 %s 网页接口获取 Token 失败：%s", platform_user_id, str(exc)[:300])
    if mode != "remote":
        detail = str(local_error)[:300]
        if "USER_VALIDATE" in detail.upper() or "ILLEGAL_ACCESS" in detail.upper():
            raise RuntimeError(
                "闲鱼要求完成设备安全验证（FAIL_SYS_USER_VALIDATE），"
                "请先在闲鱼/淘宝客户端或浏览器完成验证后重新扫码，"
                "或在系统设置中配置远程 Token"
            )
        raise RuntimeError(f"网页接口获取 Token 失败：{detail}")
    remote = await request_remote_xianyu_token(
        remote_url,
        remote_secret,
        cookies=cookie_value,
        timeout_seconds=30,
    )
    if not remote.success or not remote.token:
        detail = f"远程接口获取 Token 失败：{remote.message}"
        if local_error:
            detail = f"{detail}；网页接口：{str(local_error)[:300]}"
        if "USER_VALIDATE" in detail.upper() or "ILLEGAL_ACCESS" in detail.upper():
            detail = (
                "闲鱼要求完成设备安全验证（FAIL_SYS_USER_VALIDATE），"
                "请先在闲鱼/淘宝客户端或浏览器完成验证后重新扫码，"
                "或检查远程 Token 配置"
            )
        raise RuntimeError(detail)
    return remote.token, remote.device_id or device_id, "remote", cookie_value


def _headers_kwarg() -> str:
    try:
        return "additional_headers" if "additional_headers" in inspect.signature(websockets.connect).parameters else "extra_headers"
    except (TypeError, ValueError):
        return "additional_headers"


@dataclass
class AccountRuntime:
    account_id: str
    user_id: int | None
    cookie_value: str
    task: asyncio.Task | None = None
    websocket: Any = None
    connection_state: str = "disconnected"
    last_error: str = ""
    token_mode: str = ""
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_connected(self) -> bool:
        return self.connection_state == "connected" and self.websocket is not None

    def update(self, state: str, error: str = "") -> None:
        self.connection_state = state
        self.last_error = error[:1000]
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def serialize(self) -> dict[str, Any]:
        running = bool(self.task and not self.task.done())
        return {
            "account_id": self.account_id,
            "status": "running" if running else "stopped",
            "running": running,
            "connection_state": self.connection_state,
            "is_connected": self.is_connected,
            "cookie_loaded": bool(self.cookie_value.strip()),
            "token_mode": self.token_mode,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }

    @property
    def platform_user_id(self) -> str:
        cookies = _parse_cookie(self.cookie_value)
        return str(cookies.get("unb") or cookies.get("munb") or "")

    async def request(self, lwp: str, body: list[Any], timeout: float = 20.0) -> dict[str, Any]:
        if not self.is_connected:
            raise RuntimeError("账号尚未完成闲鱼IM连接")
        mid = _generate_mid()
        future = asyncio.get_running_loop().create_future()
        self.pending[mid] = future
        try:
            await self.websocket.send(json.dumps({"lwp": lwp, "headers": {"mid": mid}, "body": body}, ensure_ascii=False))
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.pending.pop(mid, None)

    async def send_text(self, cid: str, to_user_id: str, text: str) -> dict[str, Any]:
        payload = {"contentType": 1, "text": {"text": text}}
        data = base64.b64encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).decode()
        full_cid = cid if "@goofish" in cid else f"{cid}@goofish"
        full_to = to_user_id if "@goofish" in to_user_id else f"{to_user_id}@goofish"
        self_id = f"{self.platform_user_id}@goofish"
        body = [{
            "uuid": _generate_uuid(), "cid": full_cid, "conversationType": 1,
            "content": {"contentType": 101, "custom": {"type": 1, "data": data}},
            "redPointPolicy": 0, "extension": {"extJson": "{}"},
            "ctx": {"appVersion": "1.0", "platform": "web"}, "mtags": {},
            "msgReadStatusSetting": 1,
        }, {"actualReceivers": [full_to, self_id]}]
        response = await self.request("/r/MessageSend/sendByReceiverScope", body)
        response_body = response.get("body") or {}
        if isinstance(response_body, dict) and response_body.get("reason"):
            raise RuntimeError(str(response_body["reason"]))
        result = response_body if isinstance(response_body, dict) else {}
        # 闲鱼不同版本的发送响应字段不完全一致；统一补出消息 ID，
        # 供后端立即写入本地消息流、前端撤回和后续推送去重使用。
        return {
            **result,
            "messageId": self._message_id_from_response(response),
        }

    @staticmethod
    def _chat_id_from_response(response: dict[str, Any]) -> str:
        """从闲鱼创建会话响应的多种返回结构中提取 cid。"""
        body = response.get("body") if isinstance(response, dict) else None
        values = body if isinstance(body, list) else [body]

        def walk(value: Any) -> str:
            if isinstance(value, dict):
                for key in ("cid", "chatId", "conversationId", "id"):
                    candidate = value.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        return candidate.strip().split("@", 1)[0]
                for key in ("singleChatConversation", "singleChatUserConversation", "data"):
                    candidate = walk(value.get(key))
                    if candidate:
                        return candidate
                for candidate in value.values():
                    found = walk(candidate)
                    if found:
                        return found
            elif isinstance(value, list):
                for candidate in value:
                    found = walk(candidate)
                    if found:
                        return found
            return ""

        return walk(values)

    async def create_chat(self, to_user_id: str, item_id: str, timeout: float = 20.0) -> dict[str, Any]:
        """创建或获取与商品卖家的单聊会话。"""
        target = str(to_user_id or "").strip().split("@", 1)[0]
        item = str(item_id or "").strip()
        if not target:
            raise RuntimeError("对方用户ID不能为空")
        if not item:
            raise RuntimeError("商品ID不能为空")
        self_id = self.platform_user_id
        if not self_id:
            raise RuntimeError("当前账号 Cookie 缺少平台用户ID")
        response = await self.request(
            "/r/SingleChatConversation/create",
            [{
                "pairFirst": f"{target}@goofish",
                "pairSecond": f"{self_id}@goofish",
                "bizType": "1",
                "extension": {"itemId": item},
                "ctx": {"appVersion": "1.0", "platform": "web"},
            }],
            timeout=timeout,
        )
        reason = (response.get("body") or {}).get("reason") if isinstance(response.get("body"), dict) else ""
        if reason:
            raise RuntimeError(str(reason))
        cid = self._chat_id_from_response(response)
        if not cid:
            raise RuntimeError("闲鱼创建会话未返回有效会话ID")
        return {"cid": cid, "to_user_id": target, "item_id": item, "response": response}

    @staticmethod
    def _message_id_from_response(response: dict[str, Any]) -> str:
        body = response.get("body") or {}
        if not isinstance(body, dict):
            return ""
        value: Any = body.get("messageId") or body.get("message_id") or body.get("1")
        if isinstance(value, dict):
            value = value.get("messageId") or value.get("message_id") or value.get("1")
        return str(value or "")

    async def send_image_url(self, cid: str, to_user_id: str, cdn_url: str) -> dict[str, Any]:
        """使用已上传的闲鱼 CDN 地址发送图片消息。"""
        if not str(cdn_url or "").strip():
            raise RuntimeError("图片地址不能为空")
        payload = {
            "contentType": 2,
            "image": {"pics": [{"height": 600, "type": 0, "url": cdn_url, "width": 800}]},
        }
        data = base64.b64encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).decode()
        full_cid = cid if "@goofish" in cid else f"{cid}@goofish"
        full_to = to_user_id if "@goofish" in to_user_id else f"{to_user_id}@goofish"
        self_id = f"{self.platform_user_id}@goofish"
        body = [{
            "uuid": _generate_uuid(), "cid": full_cid, "conversationType": 1,
            "content": {"contentType": 101, "custom": {"type": 1, "data": data}},
            "redPointPolicy": 0, "extension": {"extJson": "{}"},
            "ctx": {"appVersion": "1.0", "platform": "web"}, "mtags": {},
            "msgReadStatusSetting": 1,
        }, {"actualReceivers": [full_to, self_id]}]
        response = await self.request("/r/MessageSend/sendByReceiverScope", body)
        response_body = response.get("body") or {}
        if isinstance(response_body, dict) and response_body.get("reason"):
            raise RuntimeError(str(response_body["reason"]))
        return {"imageUrl": cdn_url, "messageId": self._message_id_from_response(response), "response": response}

    async def send_image(self, cid: str, to_user_id: str, image_path: str) -> dict[str, Any]:
        """上传图片并通过与文本相同的 IM scope 发送图片消息。"""
        from common.services.xianyu_platform import upload_chat_image

        cdn_url = await upload_chat_image(image_path, self.cookie_value)
        return await self.send_image_url(cid, to_user_id, cdn_url)

    async def recall_message(self, message_id: str) -> dict[str, Any]:
        if not str(message_id or "").strip():
            raise RuntimeError("消息ID不能为空")
        response = await self.request("/r/MessageManager/recallMessage", [str(message_id)], timeout=20)
        if response.get("code") != 200:
            reason = (response.get("body") or {}).get("reason") if isinstance(response.get("body"), dict) else ""
            raise RuntimeError(str(reason or "闲鱼未确认撤回成功"))
        return response

    async def find_conversation(self, buyer_id: str, limit: int = 100) -> str:
        """按买家 ID 从闲鱼实时会话中解析可发送的 cid。"""
        target = str(buyer_id or "").split("@", 1)[0]
        if not target:
            raise RuntimeError("买家ID为空，无法查找闲鱼会话")
        result = await self.request(
            "/r/Conversation/listNewestPagination",
            [9007199254740991, max(1, min(limit, 100))],
            timeout=30,
        )
        body = result.get("body") or {}
        conversations = body.get("userConvs") if isinstance(body, dict) else []
        if not isinstance(conversations, list):
            conversations = body.get("conversations") if isinstance(body, dict) else []
        for value in conversations or []:
            if not isinstance(value, dict):
                continue
            wrapper = value.get("singleChatUserConversation") or value
            single = wrapper.get("singleChatConversation") if isinstance(wrapper, dict) else {}
            single = single if isinstance(single, dict) else {}
            pair_first = str(single.get("pairFirst") or "").split("@", 1)[0]
            pair_second = str(single.get("pairSecond") or "").split("@", 1)[0]
            cid = str(single.get("cid") or wrapper.get("cid") or value.get("cid") or "")
            if cid and target in {pair_first, pair_second}:
                return cid.split("@", 1)[0]
        raise RuntimeError("找不到该买家的闲鱼会话，请先在闲鱼中与买家产生一条消息")


class AccountRuntimeManager:
    def __init__(self) -> None:
        self._runtimes: dict[str, AccountRuntime] = {}
        self._lock = asyncio.Lock()

    async def start(self, account_id: str, cookie_value: str, user_id: int | None) -> dict[str, Any]:
        await self.stop(account_id)
        runtime = AccountRuntime(str(account_id), user_id, str(cookie_value or ""))
        runtime.update("connecting")
        runtime.task = asyncio.create_task(self._run(runtime), name=f"xianyu-account-{account_id}")
        async with self._lock:
            self._runtimes[str(account_id)] = runtime
        return runtime.serialize()

    async def stop(self, account_id: str) -> None:
        async with self._lock:
            runtime = self._runtimes.pop(str(account_id), None)
        if runtime is None:
            return
        task = runtime.task
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                logger.exception("停止账号 %s 运行时失败", account_id)

    async def get_status(self, account_id: str) -> dict[str, Any]:
        runtime = self._runtimes.get(str(account_id))
        if runtime is None:
            return {
                "account_id": str(account_id),
                "status": "not_loaded",
                "running": False,
                "connection_state": "disconnected",
                "is_connected": False,
                "cookie_loaded": False,
            }
        return runtime.serialize()

    async def request(self, account_id: str, lwp: str, body: list[Any], timeout: float = 20.0) -> dict[str, Any]:
        runtime = self._runtimes.get(str(account_id))
        if runtime is None:
            raise RuntimeError("账号运行时未加载")
        return await runtime.request(lwp, body, timeout)

    async def send_text(self, account_id: str, cid: str, to_user_id: str, text: str) -> dict[str, Any]:
        runtime = self._runtimes.get(str(account_id))
        if runtime is None:
            raise RuntimeError("账号运行时未加载")
        return await runtime.send_text(cid, to_user_id, text)

    async def create_chat(self, account_id: str, to_user_id: str, item_id: str) -> dict[str, Any]:
        runtime = self._runtimes.get(str(account_id))
        if runtime is None:
            raise RuntimeError("账号运行时未加载")
        return await runtime.create_chat(to_user_id, item_id)

    async def send_text_to_buyer(self, account_id: str, buyer_id: str, text: str) -> dict[str, Any]:
        runtime = self._runtimes.get(str(account_id))
        if runtime is None:
            raise RuntimeError("账号运行时未加载")
        cid = await runtime.find_conversation(buyer_id)
        result = await runtime.send_text(cid, buyer_id, text)
        return {"cid": cid, **result}

    async def send_image(self, account_id: str, cid: str, to_user_id: str, image_path: str) -> dict[str, Any]:
        runtime = self._runtimes.get(str(account_id))
        if runtime is None:
            raise RuntimeError("账号运行时未加载")
        return await runtime.send_image(cid, to_user_id, image_path)

    async def send_image_url(self, account_id: str, cid: str, to_user_id: str, cdn_url: str) -> dict[str, Any]:
        runtime = self._runtimes.get(str(account_id))
        if runtime is None:
            raise RuntimeError("账号运行时未加载")
        return await runtime.send_image_url(cid, to_user_id, cdn_url)

    async def recall_message(self, account_id: str, message_id: str) -> dict[str, Any]:
        runtime = self._runtimes.get(str(account_id))
        if runtime is None:
            raise RuntimeError("账号运行时未加载")
        return await runtime.recall_message(message_id)

    async def connection_stats(self) -> dict[str, Any]:
        runtimes = list(self._runtimes.values())
        by_state: dict[str, int] = {}
        connected_ids: list[str] = []
        for runtime in runtimes:
            state = runtime.connection_state
            by_state[state] = by_state.get(state, 0) + 1
            if runtime.is_connected:
                connected_ids.append(runtime.account_id)
        return {
            "total_instances": len(runtimes),
            "connected": len(connected_ids),
            "by_state": by_state,
            "connected_account_ids": connected_ids,
        }

    async def start_enabled_accounts(self) -> None:
        try:
            async with async_session_maker() as session:
                rows = (
                    await session.execute(select(Account).where(Account.status == "active"))
                ).scalars().all()
            for account in rows:
                if account.cookie:
                    await self.start(str(account.id), account.cookie, int(account.user_id))
                    await asyncio.sleep(0.05)
        except Exception:
            logger.exception("启动已启用账号运行时失败")

    async def _run(self, runtime: AccountRuntime) -> None:
        retry_delay = 5.0
        try:
            while True:
                try:
                    token, device_id, token_mode, refreshed_cookie = await _request_token(runtime.cookie_value, runtime.user_id)
                    if refreshed_cookie and refreshed_cookie != runtime.cookie_value:
                        runtime.cookie_value = refreshed_cookie
                        try:
                            async with async_session_maker() as session:
                                account = (
                                    await session.execute(select(Account).where(Account.id == int(runtime.account_id)))
                                ).scalar_one_or_none()
                                if account is not None and account.cookie != refreshed_cookie:
                                    account.cookie = refreshed_cookie
                                    await session.commit()
                        except Exception:
                            # Token 已经拿到，Cookie 写库失败不应阻断本次连接；下次
                            # 运行时仍会继续使用内存中的最新 Cookie。
                            logger.warning("账号 %s Token响应Cookie写库失败", runtime.account_id)
                    runtime.token_mode = token_mode
                    runtime.update("connecting")
                    headers = {
                        "Cookie": runtime.cookie_value.replace("\n", "").replace("\r", ""),
                    }
                    connect_kwargs = {
                        _headers_kwarg(): headers,
                        "open_timeout": 30,
                        "ping_interval": 20,
                        "ping_timeout": 15,
                    }
                    async with await websockets.connect(GOOFISH_WS_URL, **connect_kwargs) as websocket:
                        runtime.websocket = websocket
                        await websocket.send(json.dumps({
                            "lwp": "/reg",
                            "headers": {
                                "cache-header": "app-key token ua wv",
                                "app-key": WS_APP_KEY,
                                "token": token,
                                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/139 Safari/537.36",
                                "dt": "j",
                                "wv": "im:3,au:3,sy:6",
                                "sync": "0,0;0;0;",
                                "did": device_id,
                                "mid": _generate_mid(),
                            },
                        }))
                        # 注册报文需要先被闲鱼 IM 服务端处理完成，再发送
                        # ackDiff；连续发送会出现“连接状态在线但收不到实时消息”。
                        await asyncio.sleep(1.0)
                        await websocket.send(json.dumps({
                            "lwp": "/r/SyncStatus/ackDiff",
                            "headers": {"mid": _generate_mid()},
                            "body": [{
                                "pipeline": "sync", "tooLong2Tag": "PNM,1", "channel": "sync",
                                "topic": "sync", "highPts": 0, "pts": int(time.time() * 1000000),
                                "seq": 0, "timestamp": int(time.time() * 1000),
                            }],
                        }))
                        runtime.update("connected")
                        retry_delay = 5.0
                        heartbeat_task = asyncio.create_task(
                            self._heartbeat(websocket),
                            name=f"xianyu-heartbeat-{runtime.account_id}",
                        )
                        try:
                            async for raw in websocket:
                                await self._handle_message(runtime, websocket, raw)
                        finally:
                            heartbeat_task.cancel()
                            try:
                                await heartbeat_task
                            except asyncio.CancelledError:
                                pass
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    runtime.websocket = None
                    session_error = is_session_expired_message(str(exc))
                    if session_error:
                        renewed = await self._renew_runtime_account(runtime, str(exc))
                        if renewed:
                            retry_delay = 5.0
                            continue
                        retry_delay = max(retry_delay, 60.0)
                        runtime.update("expired", str(exc))
                    else:
                        runtime.update("reconnecting", str(exc))
                    logger.warning("账号 %s 长连接断开，将在 %.1f 秒后重试：%s", runtime.account_id, retry_delay, str(exc)[:300])
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60.0)
                finally:
                    runtime.websocket = None
                    if runtime.connection_state == "connected":
                        runtime.update("disconnected")
        except asyncio.CancelledError:
            runtime.websocket = None
            runtime.update("closed")
            raise
        except Exception as exc:
            runtime.websocket = None
            runtime.update("failed", str(exc))
            logger.exception("账号 %s 运行时退出", runtime.account_id)

    async def _renew_runtime_account(self, runtime: AccountRuntime, reason: str) -> bool:
        """长连接拿到 Session 过期时，在线程内直接执行一次续期。"""
        try:
            async with async_session_maker() as session:
                account = (
                    await session.execute(select(Account).where(Account.id == int(runtime.account_id)))
                ).scalar_one_or_none()
                if account is None:
                    runtime.update("expired", "账号记录不存在，无法自动续期")
                    return False
                result = await renew_account_session(
                    session,
                    account,
                    source="runtime",
                    force=True,
                    notify_runtime=False,
                    observed_session_expired=True,
                )
                if result.get("success") and account.cookie:
                    runtime.cookie_value = str(account.cookie)
                    runtime.user_id = int(account.user_id)
                    runtime.update("reconnecting", "登录态自动续期成功，正在重新连接")
                    logger.info("账号 %s 已自动续期并准备重连", runtime.account_id)
                    return True
                logger.warning(
                    "账号 %s 自动续期结果：success=%s status=%s method=%s needs_manual_login=%s message=%s",
                    runtime.account_id,
                    bool(result.get("success")),
                    str(result.get("status") or "unknown")[:40],
                    str(result.get("method") or "none")[:40],
                    bool(result.get("needs_manual_login")),
                    str(result.get("message") or reason)[:500],
                )
                runtime.update(
                    "expired" if result.get("needs_manual_login") else "reconnecting",
                    str(result.get("message") or reason)[:1000],
                )
                return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime.update("expired", f"自动续期异常：{str(exc)[:800]}")
            logger.exception("账号 %s 自动续期失败", runtime.account_id)
            return False

    async def _heartbeat(self, websocket: Any) -> None:
        try:
            while True:
                await asyncio.sleep(15)
                await websocket.send(json.dumps({"lwp": "/!", "headers": {"mid": _generate_mid()}}))
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("闲鱼IM应用心跳停止：%s", str(exc)[:200])

    async def _handle_message(self, runtime: AccountRuntime, websocket: Any, raw: Any) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict):
            return

        # 闲鱼 IM 要求对每个服务端报文回 ACK；只依赖 websockets 的 TCP ping
        # 会导致连接看似在线，但收不到 syncPushPackage。
        headers = message.get("headers") or {}
        ack_headers = {
            "mid": headers.get("mid") or _generate_mid(),
            "sid": headers.get("sid", ""),
        }
        for key in ("app-key", "ua", "dt"):
            if key in headers:
                ack_headers[key] = headers[key]
        try:
            await websocket.send(json.dumps({"code": 200, "headers": ack_headers}, ensure_ascii=False))
        except Exception:
            return

        mid = str(headers.get("mid") or "")
        future = runtime.pending.get(mid)
        if future is not None and not future.done():
            future.set_result(message)
            # 一个服务端报文可能同时是某个 LWP 请求的响应和 syncPushPackage
            # 实时推送。不能在完成 Future 后直接返回，否则新消息只会在历史
            # 接口刷新时出现，自动回复和未读实时广播都会被跳过。

        events = extract_events(message, runtime.platform_user_id)
        if events:
            logger.info("账号 %s 收到 %s 条实时消息事件", runtime.account_id, len(events))
        for event in events:
            await self._publish_event(runtime, event)

    async def _publish_event(self, runtime: AccountRuntime, event: dict[str, Any]) -> None:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.post(
                    f"{settings.backend_service_url.rstrip('/')}/api/v1/chat-new/internal/events",
                    headers={"X-Internal-Token": settings.jwt_secret},
                    json={"account_id": int(runtime.account_id), "event": event},
                )
                if not response.is_success:
                    logger.warning("账号 %s 实时消息回传失败：HTTP %s", runtime.account_id, response.status_code)
                else:
                    message = event.get("message") or {}
                    logger.info(
                        "账号 %s 实时消息已回传：cid=%s，message_id=%s",
                        runtime.account_id,
                        str(event.get("cid") or "")[:80],
                        str(message.get("messageId") or "")[:80],
                    )
        except Exception as exc:
            logger.warning("账号 %s 实时消息回传异常：%s", runtime.account_id, str(exc)[:300])


runtime_manager = AccountRuntimeManager()
