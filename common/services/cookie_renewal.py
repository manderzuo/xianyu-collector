# -*- coding: utf-8 -*-
"""闲鱼登录 Cookie 自动续期。

续期链路与旧版保持同一语义：

1. ``hasLogin.do`` 刷新 Web 登录 Cookie；
2. ``silentHasLogin.do`` 刷新短期登录 Cookie；
3. ``setLoginSettings.do`` 刷新长登录 Cookie；
4. 接口续期失败后委托 WebSocket 服务使用持久化浏览器续期；
5. 浏览器续期后再次用接口确认长登录 Cookie。

本模块只负责平台协议，不直接修改数据库。数据库保存和运行时重启由
``account_renewal`` 统一处理，避免 scheduler、backend、websocket 各自维护
一套不一致的 Cookie 合并逻辑。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from common.config import settings
from common.services.goofish_mtop import parse_cookie_string, serialize_cookies

logger = logging.getLogger("xr.cookie_renewal")

SESSION_MARKERS = (
    "FAIL_SYS_SESSION_EXPIRED",
    # 闲鱼 IM Token 过期时返回这两个业务码（EXOIRED 是平台实际拼写）。
    # 对运行时而言同样代表当前登录态需要续期，否则会一直重试取 Token。
    "FAIL_SYS_TOKEN_EXOIRED",
    "FAIL_SYS_TOKEN_EXPIRED",
    # 闲鱼会在 Cookie 看似仍有效、但设备校验或风控票据已经失效时返回
    # USER_VALIDATE。此时继续用原 Cookie 获取 IM Token 只会无限重试，
    # 应进入受冷却保护的自动续期流程。
    "FAIL_SYS_USER_VALIDATE",
    "FAIL_SYS_ILLEGAL_ACCESS",
    "FAIL_BIZ_WUA_IS_MACHINE",
    "WUA_IS_MACHINE",
    "SESSION过期",
    "Token过期",
    "令牌过期",
    "登录态已失效",
    "Cookie失效",
    "未登录",
)

BROWSER_RECOVERY_MARKERS = (
    "FAIL_SYS_USER_VALIDATE",
    "FAIL_SYS_ILLEGAL_ACCESS",
    "FAIL_BIZ_WUA_IS_MACHINE",
    "WUA_IS_MACHINE",
)


@dataclass(slots=True)
class CookieRenewalResult:
    success: bool
    new_cookie: str
    updated_cookie_names: list[str] = field(default_factory=list)
    method: str = "none"
    message: str = ""
    needs_manual_login: bool = False
    response_text: str = ""
    steps: list[str] = field(default_factory=list)


def is_session_expired_message(value: str | None) -> bool:
    text = str(value or "").lower()
    return any(marker.lower() in text for marker in SESSION_MARKERS)


def requires_browser_recovery_message(value: str | None) -> bool:
    """设备校验/风控错误无法只靠 Passport 接口刷新来恢复。"""
    text = str(value or "").lower()
    return any(marker.lower() in text for marker in BROWSER_RECOVERY_MARKERS)


def _headers(cookie_value: str, *, referer: str = "https://www.goofish.com/") -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www.goofish.com",
        "Pragma": "no-cache",
        "Referer": referer,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36"
        ),
        "Cookie": str(cookie_value or "").replace("\n", "").replace("\r", ""),
    }


def _set_cookie_headers(response: httpx.Response) -> list[str]:
    return list(response.headers.get_list("set-cookie"))


def _merge_set_cookies(original: str, headers: list[str]) -> tuple[str, list[str]]:
    merged = parse_cookie_string(original)
    updated: list[str] = []
    for raw in headers:
        if not raw or "=" not in raw:
            continue
        pair = raw.split(";", 1)[0].strip()
        name, value = pair.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or "Max-Age=0" in raw or "1970" in raw:
            continue
        if merged.get(name) != value and name not in updated:
            updated.append(name)
        merged[name] = value
    return serialize_cookies(merged), updated


def _json_message(text: str, *, success_message: str) -> tuple[bool, str]:
    try:
        payload = json.loads(text or "{}")
    except (TypeError, ValueError):
        return False, "接口返回内容不是 JSON"
    content = payload.get("content") if isinstance(payload, dict) else None
    if isinstance(content, dict):
        if bool(content.get("success")):
            return True, success_message
        message = content.get("titleMsg") or content.get("retMsg") or content.get("msg") or content.get("code")
        if message:
            return False, str(message)
    ret = payload.get("ret") if isinstance(payload, dict) else None
    if isinstance(ret, list) and ret:
        return False, str(ret[0])
    if isinstance(ret, str) and ret:
        return False, ret
    return False, "接口返回业务失败"


class CookieRenewalService:
    """调用闲鱼 Passport 续期接口并在失败时委托浏览器续期。"""

    timeout = httpx.Timeout(connect=15, read=30, write=20, pool=30)

    async def renew(
        self,
        cookie_value: str,
        account_id: str = "",
        *,
        allow_browser: bool = True,
        username: str = "",
        password: str = "",
        show_browser: bool = False,
        force_browser: bool = False,
    ) -> CookieRenewalResult:
        original = str(cookie_value or "").strip()
        prefix = f"账号 {account_id}" if account_id else "账号"
        if not original or not parse_cookie_string(original):
            return CookieRenewalResult(
                success=False,
                new_cookie=original,
                message="Cookie为空或格式无效，请重新扫码登录",
                needs_manual_login=True,
                steps=["接口续期跳过：Cookie为空或格式无效"],
            )

        api_result = await self._api_renew_with_retry(original, prefix)
        if api_result["success"] and not force_browser:
            return CookieRenewalResult(
                success=True,
                new_cookie=api_result["new_cookie"],
                updated_cookie_names=api_result["updated_names"],
                method="api",
                message=api_result["message"],
                response_text=api_result["response_text"],
                steps=api_result["steps"],
            )

        if force_browser:
            api_result["steps"].append("检测到设备校验错误，继续执行浏览器续期")

        if not allow_browser:
            return CookieRenewalResult(
                success=False,
                new_cookie=api_result["new_cookie"],
                updated_cookie_names=api_result["updated_names"],
                message=f"接口续期失败：{api_result['message']}",
                response_text=api_result["response_text"],
                needs_manual_login=True,
                steps=api_result["steps"] + ["浏览器续期已关闭"],
            )

        browser = await self._browser_renew(api_result["new_cookie"] or original, account_id, prefix)
        if browser["success"]:
            verify = await self._api_renew_with_retry(browser["new_cookie"], f"{prefix}[浏览器后验证]")
            if verify["success"]:
                updated = self._updated_names(original, verify["new_cookie"])
                return CookieRenewalResult(
                    success=True,
                    new_cookie=verify["new_cookie"],
                    updated_cookie_names=updated,
                    method="browser+api",
                    message="接口续期失败，浏览器续期成功并完成接口确认",
                    response_text=verify["response_text"],
                    steps=api_result["steps"] + browser["steps"] + verify["steps"],
                )
            browser_cookie = browser["new_cookie"]
            password_result = await self._password_renew(
                browser_cookie,
                account_id,
                username,
                password,
                show_browser,
                prefix,
            )
            if password_result["success"]:
                updated = self._updated_names(original, password_result["new_cookie"])
                return CookieRenewalResult(
                    success=True,
                    new_cookie=password_result["new_cookie"],
                    updated_cookie_names=updated,
                    method="password",
                    message=password_result["message"],
                    steps=api_result["steps"] + browser["steps"] + verify["steps"] + password_result["steps"],
                )
            return CookieRenewalResult(
                success=False,
                new_cookie=browser_cookie,
                updated_cookie_names=self._updated_names(original, browser_cookie),
                method="password" if password_result["attempted"] else "browser",
                message=(
                    f"浏览器已拿到新 Cookie，但长登录确认失败：{verify['message']}；"
                    f"{password_result['message']}"
                ),
                response_text=verify["response_text"],
                needs_manual_login=True,
                steps=api_result["steps"] + browser["steps"] + verify["steps"] + password_result["steps"],
            )

        password_result = await self._password_renew(
            api_result["new_cookie"] or original,
            account_id,
            username,
            password,
            show_browser,
            prefix,
        )
        if password_result["success"]:
            updated = self._updated_names(original, password_result["new_cookie"])
            return CookieRenewalResult(
                success=True,
                new_cookie=password_result["new_cookie"],
                updated_cookie_names=updated,
                method="password",
                message=password_result["message"],
                steps=api_result["steps"] + browser["steps"] + password_result["steps"],
            )
        return CookieRenewalResult(
            success=False,
            new_cookie=api_result["new_cookie"] or original,
            updated_cookie_names=api_result["updated_names"],
            method="password" if password_result["attempted"] else "none",
            message=(
                f"接口续期失败：{api_result['message']}；浏览器续期失败：{browser['message']}；"
                f"{password_result['message']}"
            ),
            response_text=api_result["response_text"],
            needs_manual_login=True,
            steps=api_result["steps"] + browser["steps"] + password_result["steps"],
        )

    async def _api_renew_with_retry(self, cookie_value: str, prefix: str) -> dict[str, Any]:
        result = await self._api_renew_once(cookie_value, prefix)
        if result["success"] or result["new_cookie"] == cookie_value:
            return result
        # Passport 偶尔第一次只下发短期 Cookie，旧版会再次调用确认长登录。
        await asyncio.sleep(1)
        retry = await self._api_renew_once(result["new_cookie"] or cookie_value, f"{prefix}[重试]")
        if retry["success"]:
            return retry
        retry["steps"] = result["steps"] + retry["steps"]
        return retry

    async def _api_renew_once(self, cookie_value: str, prefix: str) -> dict[str, Any]:
        current = cookie_value
        steps: list[str] = []
        response_text = ""
        all_headers: list[str] = []
        long_headers: list[str] = []
        cookies = parse_cookie_string(current)
        if not cookies.get("unb"):
            return {
                "success": False,
                "new_cookie": current,
                "updated_names": [],
                "message": "Cookie缺少 unb，无法续期",
                "response_text": "",
                "steps": ["接口续期失败：Cookie缺少 unb"],
            }

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            proxy=settings.goofish_proxy or None,
        ) as client:
            # 1) Web 登录态确认。
            try:
                now_ms = str(int(time.time() * 1000))
                page_trace_id = f"21504{now_ms}{random.randint(100000, 999999)}"
                rnd = str(random.random())
                body = {
                    "hid": cookies.get("unb", ""),
                    "ltl": "true",
                    "appName": "xianyu",
                    "appEntrance": "web",
                    "_csrf_token": cookies.get("_tb_token_", ""),
                    "umidToken": cookies.get("_uab_collina", "") or cookies.get("cna", ""),
                    "hsiz": cookies.get("cookie2", ""),
                    "bizParams": "taobaoBizLoginFrom=web&renderRefer=https%3A%2F%2Fwww.goofish.com%2F",
                    "mainPage": "false",
                    "isMobile": "false",
                    "lang": "zh_CN",
                    "returnUrl": "",
                    "fromSite": "77",
                    "isIframe": "true",
                    "documentReferer": "https://www.goofish.com/",
                    "defaultView": "hasLogin",
                    "umidTag": "SERVER",
                    "deviceId": "",
                    "pageTraceId": page_trace_id,
                }
                headers = _headers(
                    current,
                    referer=(
                        "https://passport.goofish.com/mini_login.htm?lang=zh_cn&appName=xianyu&"
                        f"appEntrance=web&styleType=vertical&notKeepLogin=false&stie=77&rnd={rnd}"
                    ),
                )
                if cookies.get("XSRF-TOKEN"):
                    headers["X-XSRF-TOKEN"] = cookies["XSRF-TOKEN"]
                response = await client.post(
                    f"{settings.goofish_passport_host}/newlogin/hasLogin.do",
                    params={"appName": "xianyu", "fromSite": "77"},
                    data=body,
                    headers=headers,
                )
                current, _ = _merge_set_cookies(current, _set_cookie_headers(response))
                all_headers.extend(_set_cookie_headers(response))
                steps.append(f"hasLogin.do：HTTP {response.status_code}")
            except (httpx.HTTPError, ValueError) as exc:
                steps.append(f"hasLogin.do失败：{str(exc)[:180]}")

            # 2) 短期登录态确认。
            try:
                params = {
                    "documentReferer": "https://www.goofish.com/",
                    "appName": "xianyu",
                    "appEntrance": "xianyu_sdkSilent",
                    "fromSite": "0",
                    "ltl": "true",
                }
                response = await client.post(
                    f"{settings.goofish_passport_host}/newlogin/silentHasLogin.do",
                    params=params,
                    headers=_headers(current),
                )
                response_text = response.text or ""
                set_headers = _set_cookie_headers(response)
                current, _ = _merge_set_cookies(current, set_headers)
                all_headers.extend(set_headers)
                silent_ok, silent_message = _json_message(response_text, success_message="silentHasLogin调用成功")
                steps.append(f"silentHasLogin.do：{'成功' if silent_ok else silent_message[:120]}")
            except (httpx.HTTPError, ValueError) as exc:
                silent_message = str(exc)
                steps.append(f"silentHasLogin.do失败：{silent_message[:180]}")

            # 3) 长登录 Cookie。
            try:
                response = await client.post(
                    f"{settings.goofish_passport_host}/ac/account/setLoginSettings.do",
                    params={"fromSite": "77", "appName": "xianyu", "bizEntrance": "web"},
                    data={"status": "0"},
                    headers=_headers(current),
                )
                long_headers = [
                    item for item in _set_cookie_headers(response)
                    if "Max-Age=0" not in item and "1970" not in item
                ]
                all_headers.extend(long_headers)
                steps.append(f"setLoginSettings.do：收到 {len(long_headers)} 个长登录 Cookie")
            except (httpx.HTTPError, ValueError) as exc:
                steps.append(f"setLoginSettings.do失败：{str(exc)[:180]}")

        new_cookie, updated_names = _merge_set_cookies(cookie_value, all_headers)
        # 只有 setLoginSettings 实际返回长登录 Set-Cookie 才算 API 续期成功，
        # 这与旧版一致，避免把只刷新短期 Cookie 当作完整登录恢复。
        success = bool(long_headers and parse_cookie_string(new_cookie).get("unb"))
        message = "接口续期成功" if success else (silent_message if "silent_message" in locals() else "未收到长登录 Cookie")
        if not all_headers:
            message = message or "接口未返回 Set-Cookie"
        return {
            "success": success,
            "new_cookie": new_cookie,
            "updated_names": updated_names,
            "message": message[:500],
            "response_text": response_text[:2000],
            "steps": steps,
        }

    async def _browser_renew(self, cookie_value: str, account_id: str, prefix: str) -> dict[str, Any]:
        base = settings.websocket_service_url.rstrip("/")
        if not base:
            return {"success": False, "new_cookie": cookie_value, "message": "未配置 WebSocket 服务", "steps": ["浏览器续期跳过：未配置 WebSocket 服务"]}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=20, pool=30)) as client:
                response = await client.post(
                    f"{base}/internal/cookies/browser-renew",
                    json={"account_id": str(account_id), "cookies_str": cookie_value},
                    headers={"X-Internal-Token": settings.jwt_secret},
                )
            payload = response.json()
            data = payload.get("data") or {}
            success = bool(payload.get("success")) and bool(data.get("new_cookies_str"))
            message = str(payload.get("message") or data.get("message") or "浏览器续期失败")
            return {
                "success": success,
                "new_cookie": str(data.get("new_cookies_str") or cookie_value),
                "message": message[:500],
                "steps": [f"浏览器续期：{message[:180]}"],
            }
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("%s 浏览器续期请求失败：%s", prefix, str(exc)[:300])
            return {
                "success": False,
                "new_cookie": cookie_value,
                "message": f"浏览器续期服务不可用：{str(exc)[:300]}",
                "steps": [f"浏览器续期请求失败：{str(exc)[:180]}"],
            }

    async def _password_renew(
        self,
        cookie_value: str,
        account_id: str,
        username: str,
        password: str,
        show_browser: bool,
        prefix: str,
    ) -> dict[str, Any]:
        """把账号密码登录委托给 WebSocket 的持久化浏览器。"""
        if not str(username or "").strip() or not str(password or ""):
            return {
                "attempted": False,
                "success": False,
                "new_cookie": cookie_value,
                "message": "未配置账号密码，无法自动登录；请重新扫码登录",
                "steps": ["密码登录跳过：未配置账号密码"],
            }
        base = settings.websocket_service_url.rstrip("/")
        if not base:
            return {
                "attempted": True,
                "success": False,
                "new_cookie": cookie_value,
                "message": "未配置 WebSocket 服务，无法执行密码登录",
                "steps": ["密码登录失败：未配置 WebSocket 服务"],
            }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=20, pool=30)) as client:
                response = await client.post(
                    f"{base}/internal/cookies/password-renew",
                    json={
                        "account_id": str(account_id),
                        "cookies_str": cookie_value,
                        "username": str(username).strip(),
                        "password": str(password),
                        "show_browser": bool(show_browser),
                    },
                    headers={"X-Internal-Token": settings.jwt_secret},
                )
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else {}
            data = data if isinstance(data, dict) else {}
            success = bool(response.is_success and payload.get("success") and data.get("new_cookies_str"))
            message = str(payload.get("message") or data.get("message") or "密码登录失败")
            return {
                "attempted": True,
                "success": success,
                "new_cookie": str(data.get("new_cookies_str") or cookie_value),
                "message": message[:500],
                "steps": [f"密码登录：{message[:180]}"],
            }
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("%s 密码登录请求失败：%s", prefix, str(exc)[:300])
            return {
                "attempted": True,
                "success": False,
                "new_cookie": cookie_value,
                "message": f"密码登录服务不可用：{str(exc)[:300]}",
                "steps": [f"密码登录请求失败：{str(exc)[:180]}"],
            }

    @staticmethod
    def _updated_names(original: str, new: str) -> list[str]:
        old = parse_cookie_string(original)
        current = parse_cookie_string(new)
        return [name for name, value in current.items() if old.get(name) != value]


cookie_renewal_service = CookieRenewalService()

__all__ = [
    "CookieRenewalResult",
    "CookieRenewalService",
    "cookie_renewal_service",
    "is_session_expired_message",
    "requires_browser_recovery_message",
]
