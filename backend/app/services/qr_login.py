"""闲鱼真实二维码登录协议。

该服务只负责与闲鱼登录页通信和维护短期会话，账号落库由 API 路由完成。
这样可以把协议状态和业务账号生命周期分开，也便于前端安全地轮询状态。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from io import BytesIO
from random import random
from typing import Any

import httpx
import qrcode
import qrcode.constants

from common.config import settings


def _headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Referer": f"{settings.goofish_passport_host}/",
        "Origin": settings.goofish_passport_host,
    }


@dataclass
class QRLoginSession:
    session_id: str
    proxy: str | None = None
    status: str = "waiting"
    qr_code_url: str | None = None
    qr_content: str | None = None
    cookies: dict[str, str] = field(default_factory=dict)
    unb: str | None = None
    nickname: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    expires_in: int = 300
    verification_url: str | None = None
    face_qr_url: str | None = None
    error: str | None = None
    monitor_task: asyncio.Task | None = None

    @property
    def expires_at(self) -> float:
        return self.created_at + self.expires_in

    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    def marshal_cookies(self) -> str:
        return "; ".join(f"{key}={value}" for key, value in self.cookies.items())


class QRLoginManager:
    """管理二维码生成与闲鱼登录状态轮询。"""

    def __init__(self) -> None:
        self.sessions: dict[str, QRLoginSession] = {}
        self._tasks: set[asyncio.Task] = set()
        self.headers = _headers()
        host = settings.goofish_passport_host
        self.api_mini_login = f"{host}/mini_login.htm"
        self.api_generate_qr = f"{host}/newlogin/qrcode/generate.do"
        self.api_scan_status = f"{host}/newlogin/qrcode/query.do"
        self.api_face_check = f"{host}/iv/photoVerify/check.do"
        self.api_mtop = f"{settings.goofish_mtop_host}/h5/mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get/1.0/"
        self.timeout = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=60.0)

    def _client(self, proxy: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.timeout,
            proxy=proxy or settings.goofish_proxy or None,
        )

    @staticmethod
    def _update_cookies(session: QRLoginSession, response: httpx.Response) -> None:
        for key, value in response.cookies.items():
            session.cookies[key] = value
            if key == "unb":
                session.unb = value

    @staticmethod
    def _extract_nickname(value: Any) -> str:
        """从登录收口接口的不同响应包装中提取平台昵称。"""
        nickname_keys = ("nick", "nickname", "nickName", "userNick", "userName")
        if isinstance(value, dict):
            for key in nickname_keys:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip() and not candidate.strip().isdigit():
                    return candidate.strip()[:64]
            for child in value.values():
                result = QRLoginManager._extract_nickname(child)
                if result:
                    return result
        elif isinstance(value, list):
            for child in value:
                result = QRLoginManager._extract_nickname(child)
                if result:
                    return result
        return ""

    async def _get_login_token(self, session: QRLoginSession) -> None:
        """访问 mtop 首页，获取登录接口需要的 m_h5_tk。"""
        data = {"bizScene": "home"}
        data_string = json.dumps(data, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        app_key = "34839810"
        async with self._client(session.proxy) as client:
            response = await client.get(self.api_mtop, headers=self.headers)
            self._update_cookies(session, response)
            token_cookie = session.cookies.get("m_h5_tk", "")
            token = token_cookie.split("_", 1)[0] if "_" in token_cookie else ""
            sign = hashlib.md5(f"{token}&{timestamp}&{app_key}&{data_string}".encode()).hexdigest()
            params = {
                "jsv": "2.7.2", "appKey": app_key, "t": timestamp, "sign": sign,
                "v": "1.0", "type": "originaljson", "dataType": "json",
                "timeout": 20000, "api": "mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get",
                "data": data_string,
            }
            response = await client.post(self.api_mtop, params=params, headers=self.headers, cookies=session.cookies)
            self._update_cookies(session, response)

    async def _get_login_params(self, session: QRLoginSession) -> None:
        params = {
            "lang": "zh_cn", "appName": "xianyu", "appEntrance": "web",
            "styleType": "vertical", "bizParams": "", "notLoadSsoView": False,
            "notKeepLogin": False, "isMobile": False, "qrCodeFirst": False,
            "stie": 77, "rnd": random(),
        }
        async with self._client(session.proxy) as client:
            response = await client.get(self.api_mini_login, params=params, cookies=session.cookies, headers=self.headers)
            # mini_login.htm 会补发 passport 域的 XSRF-TOKEN、_tb_token_ 等
            # Cookie。旧版 requests.Session 会自动合并，这里使用 httpx 的
            # 手动 Cookie 字典，必须显式合并，否则 query.do 可能只返回 NEW。
            self._update_cookies(session, response)
        match = re.search(r"window\.viewData\s*=\s*(\{.*?\});", response.text, re.S)
        if not match:
            raise RuntimeError("登录页未返回 loginFormData，可能被网络代理或平台风控拦截")
        view_data = json.loads(match.group(1))
        login_data = view_data.get("loginFormData")
        if not isinstance(login_data, dict):
            raise RuntimeError("登录页缺少 loginFormData")
        login_data["umidTag"] = "SERVER"
        # 不同时间段的登录页返回字段不完全一致。以登录页返回值为主，
        # 对旧版协议明确依赖的字段提供同等的兜底，避免平台灰度字段缺失。
        login_data.setdefault("fromSite", "77")
        login_data.setdefault("_csrf_token", session.cookies.get("XSRF-TOKEN", ""))
        login_data.setdefault("hsiz", session.cookies.get("cookie2", ""))
        login_data.setdefault("umidToken", "")
        login_data.setdefault("mainPage", "false")
        login_data.setdefault("isIframe", "true")
        login_data.setdefault("navlanguage", "en")
        login_data.setdefault("navUserAgent", self.headers.get("User-Agent", ""))
        login_data.setdefault("navPlatform", "Win32")
        login_data.setdefault("deviceId", session.cookies.get("cna", ""))
        session.params.update(login_data)

    @staticmethod
    def _render_qr(content: str) -> str:
        qr = qrcode.QRCode(
            version=5,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=2,
        )
        qr.add_data(content)
        qr.make(fit=True)
        image = qr.make_image()
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

    async def generate_qr_code(self, proxy: str | None = None) -> dict[str, Any]:
        session = QRLoginSession(session_id=str(uuid.uuid4()), proxy=proxy)
        try:
            await self._get_login_token(session)
            await self._get_login_params(session)
            async with self._client(session.proxy) as client:
                response = await client.get(self.api_generate_qr, params=session.params, headers=self.headers, cookies=session.cookies)
            self._update_cookies(session, response)
            payload = response.json()
            content = payload.get("content", {})
            data = content.get("data", {}) if isinstance(content, dict) else {}
            if content.get("success") is not True or not data.get("codeContent"):
                raise RuntimeError("闲鱼二维码接口未返回有效二维码")
            session.params.update({"t": data.get("t"), "ck": data.get("ck")})
            session.qr_content = str(data["codeContent"])
            session.qr_code_url = self._render_qr(session.qr_content)
            self.sessions[session.session_id] = session
            task = asyncio.create_task(self._monitor(session.session_id))
            session.monitor_task = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return {
                "success": True,
                "session_id": session.session_id,
                "qr_code_url": session.qr_code_url,
                "expires_in": session.expires_in,
            }
        except httpx.ConnectTimeout:
            return {"success": False, "message": "连接闲鱼登录服务超时，请检查网络或代理"}
        except httpx.ReadTimeout:
            return {"success": False, "message": "闲鱼登录服务响应超时"}
        except httpx.HTTPError as exc:
            return {"success": False, "message": f"连接闲鱼登录服务失败：{exc}"}
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            return {"success": False, "message": str(exc)}
        except Exception as exc:  # pragma: no cover - 平台返回异常时的兜底
            return {"success": False, "message": f"生成二维码失败：{exc}"}

    async def _poll(self, session: QRLoginSession) -> dict[str, Any]:
        async with self._client(session.proxy) as client:
            # 旧版 query.do 同时携带 URL 查询参数和 form body；部分网关只
            # 在 URL 参数中读取 appName/fromSite，缺一时会一直返回 NEW。
            response = await client.post(
                self.api_scan_status,
                params={"appName": "xianyu", "fromSite": "77"},
                data=session.params,
                cookies=session.cookies,
                headers={**self.headers, "Content-Type": "application/x-www-form-urlencoded"},
            )
        self._update_cookies(session, response)
        payload = response.json()
        content = payload.get("content", {})
        return content.get("data", {}) if isinstance(content, dict) else {}

    async def _run_face_verification(self, session: QRLoginSession) -> None:
        """复刻旧版风控人脸分支，直到拿到最终登录 Cookie。"""
        if not session.verification_url:
            raise RuntimeError("闲鱼要求人脸验证，但没有返回验证地址")
        async with self._client(session.proxy) as client:
            response = await client.get(session.verification_url, cookies=session.cookies, headers=self.headers)
            self._update_cookies(session, response)
            normal_html = response.text
            token_match = re.search(r"htoken=([A-Za-z0-9_\-]+)", normal_html)
            if not token_match:
                raise RuntimeError("人脸验证页面未返回 htoken")
            htoken = token_match.group(1)
            modes_match = re.search(
                r'window\.location\.href\s*=\s*"(https://[^\"]*?/iv/mini/verify_modes\.htm\?[^\"]*)"',
                normal_html,
            )
            if not modes_match:
                raise RuntimeError("人脸验证页面未返回验证模式地址")
            verify_modes_url = modes_match.group(1)
            if verify_modes_url.endswith("_umidfg="):
                verify_modes_url += "1"
            response = await client.get(verify_modes_url, cookies=session.cookies, headers=self.headers)
            self._update_cookies(session, response)
            face_match = re.search(r'new\s+Qrcode\(\{\s*text:\s*"([^\"]+)"', response.text)
            if not face_match:
                raise RuntimeError("人脸验证页面未返回二维码")
            session.face_qr_url = self._render_qr(face_match.group(1))
            check_headers = {
                **self.headers,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{settings.goofish_passport_host}/iv/mini/identity_verify.htm?htoken={htoken}",
            }
            verify_url: str | None = None
            while not session.is_expired():
                response = await client.get(self.api_face_check, params={"htoken": htoken}, headers=check_headers, cookies=session.cookies)
                self._update_cookies(session, response)
                content = response.json().get("content", {})
                code = str(content.get("code", "")) if isinstance(content, dict) else ""
                if code == "3":
                    verify_url = content.get("url")
                    break
                if code not in {"0", ""}:
                    session.error = f"人脸验证返回异常状态：{code}"
                await asyncio.sleep(2)
            if not verify_url:
                raise RuntimeError("人脸验证超时或未完成")
            response = await client.get(verify_url, headers=check_headers, cookies=session.cookies)
            self._update_cookies(session, response)
        await self._finalize_login(session, {})
        if not session.unb:
            raise RuntimeError("人脸验证完成，但没有拿到闲鱼账号标识")

    async def _finalize_login(self, session: QRLoginSession, data: dict[str, Any]) -> None:
        """使用 query.do 返回的令牌完成登录收口并刷新最终 Cookie。"""
        login_token = data.get("token") or data.get("lgToken")
        if login_token:
            async with self._client(session.proxy) as client:
                response = await client.post(
                    f"{settings.goofish_passport_host}/login_token/login.do",
                    params={"token": login_token, "subFlow": "DIALOG_CHECK_LOGIN_RPC", "nextCode": "0018", "bizScene": "qrcode", "confirm": "true"},
                    data={"deviceId": session.cookies.get("cna", "")},
                    headers={**self.headers, "Content-Type": "application/x-www-form-urlencoded"},
                    cookies=session.cookies,
                )
                self._update_cookies(session, response)
        # 登录收口后访问用户导航接口，确保 unb / tracknick 等最终 Cookie 已下发。
        timestamp = str(int(time.time() * 1000))
        params = {
            "jsv": "2.7.2", "appKey": "34839810", "t": timestamp, "sign": "",
            "v": "1.0", "type": "originaljson", "dataType": "json", "timeout": "20000",
            "api": "mtop.idle.web.user.page.nav", "sessionOption": "AutoLoginOnly", "spm_cnt": "a21ybx.home.0.0",
        }
        async with self._client(session.proxy) as client:
            response = await client.post(
                f"{settings.goofish_mtop_host}/h5/mtop.idle.web.user.page.nav/1.0/",
                params=params,
                data={"data": "{}"},
                headers=self.headers,
                cookies=session.cookies,
            )
            self._update_cookies(session, response)
            try:
                session.nickname = self._extract_nickname(response.json()) or session.nickname
            except ValueError:
                pass

    async def _monitor(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        while not session.is_expired() and session.status in {"waiting", "scanned"}:
            try:
                data = await self._poll(session)
                qr_status = data.get("qrCodeStatus")
                if qr_status == "CONFIRMED":
                    if data.get("iframeRedirect") is True:
                        session.status = "verification_required"
                        session.verification_url = data.get("iframeRedirectUrl")
                        session.created_at = time.time()
                        await self._run_face_verification(session)
                        session.status = "success"
                    else:
                        await self._finalize_login(session, data)
                        session.status = "success"
                    break
                if qr_status == "SCANED":
                    session.status = "scanned"
                elif qr_status == "EXPIRED":
                    session.status = "expired"
                    break
                elif qr_status not in {"NEW", "SCANED", "CONFIRMED"}:
                    session.status = "cancelled"
                    break
                await asyncio.sleep(0.8)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                session.error = str(exc)
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 平台偶发返回异常不应让任务静默退出
                session.error = str(exc)
                await asyncio.sleep(2)
        if session.status in {"waiting", "scanned"}:
            session.status = "expired"

    def status(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None:
            return {"status": "not_found", "session_id": session_id}
        if session.is_expired() and session.status not in {"success", "verification_required"}:
            session.status = "expired"
        data: dict[str, Any] = {"status": session.status, "session_id": session.session_id}
        if session.status == "verification_required":
            data.update({"verification_url": session.verification_url, "face_qr_url": session.face_qr_url, "message": "需要人脸验证，请按提示完成验证"})
        if session.error:
            data["error"] = session.error
        return data

    def cookies(self, session_id: str) -> dict[str, str] | None:
        session = self.sessions.get(session_id)
        if session is None or session.status != "success":
            return None
        return {
            "cookies": session.marshal_cookies(),
            "unb": session.unb or "",
            "nickname": session.nickname or "",
        }

    def cancel(self, session_id: str) -> bool:
        session = self.sessions.get(session_id)
        if session is None:
            return False
        session.status = "cancelled"
        if session.monitor_task and not session.monitor_task.done():
            session.monitor_task.cancel()
        return True

    def cleanup(self) -> None:
        for session_id, session in list(self.sessions.items()):
            if session.is_expired() and session.status != "success":
                self.sessions.pop(session_id, None)


qr_login_manager = QRLoginManager()
