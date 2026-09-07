# -*- coding: utf-8 -*-
"""在 WebSocket 进程中用持久化浏览器续期 Cookie。"""
from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from common.config import settings
from common.services.goofish_mtop import parse_cookie_string, serialize_cookies

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - 由部署镜像按需安装
    sync_playwright = None


_account_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _cookie_payloads(cookie_value: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name, value in parse_cookie_string(cookie_value).items():
        for domain in (".goofish.com", ".taobao.com", ".alipay.com"):
            result.append({"name": name, "value": value, "domain": domain, "path": "/"})
    return result


def _merge_browser_cookies(original: str, browser_cookies: list[dict[str, Any]]) -> str:
    merged = parse_cookie_string(original)
    for item in browser_cookies:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if name and value:
            merged[name] = value
    return serialize_cookies(merged)


def _sync_renew(cookie_value: str, account_id: str) -> dict[str, Any]:
    if sync_playwright is None:
        return {"success": False, "message": "WebSocket 服务未安装 Playwright，无法执行浏览器续期"}
    if not cookie_value.strip():
        return {"success": False, "message": "Cookie为空，无法执行浏览器续期"}

    base_dir = Path(os.getenv("BROWSER_DATA_DIR", "./browser_data"))
    user_dir = base_dir / f"user_{account_id or 'unknown'}"
    user_dir.mkdir(parents=True, exist_ok=True)
    playwright = None
    context = None
    try:
        playwright = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "headless": os.getenv("BROWSER_HEADLESS", "true").lower() != "false",
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-notifications",
                "--lang=zh-CN",
            ],
            "viewport": {"width": 1280, "height": 720},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/139.0.0.0 Safari/537.36"
            ),
        }
        context = playwright.chromium.launch_persistent_context(str(user_dir), **launch_kwargs)
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        payloads = _cookie_payloads(cookie_value)
        if payloads:
            context.add_cookies(payloads)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.goofish.com/im", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        quick_enter = False
        # 快速进入分支也会继续走统一的登录态判断；必须先初始化，
        # 否则点击成功后会直接引用未赋值的 logged_in。
        logged_in = False
        frames = [page, *page.frames]
        for frame in frames:
            for selector in (
                'button:has-text("快速进入")',
                'button[type="submit"]:has-text("快速进入")',
                '.fm-button:has-text("快速进入")',
                '.fn-button:has-text("快速进入")',
            ):
                try:
                    locator = frame.locator(selector)
                    if locator.count() > 0 and locator.first.is_visible():
                        locator.first.click(timeout=5000)
                        quick_enter = True
                        break
                except Exception:
                    continue
            if quick_enter:
                break
        if quick_enter:
            page.wait_for_timeout(5000)
            # 快速进入按钮只会在持久化浏览器已有登录态时出现，点击成功
            # 即表示已完成进入；后续仍会通过接口确认拿到的 Cookie。
            logged_in = True
        else:
            for selector in (
                "div.nick",
                ".header-right .nick",
                ".rc-virtual-list-holder-inner",
                'img[src*="img.alicdn.com"]',
                ".nc-container",
            ):
                try:
                    locator = page.locator(selector)
                    if locator.count() > 0 and locator.first.is_visible():
                        logged_in = True
                        break
                except Exception:
                    continue
            if not logged_in:
                body_text = page.locator("body").text_content() or ""
                logged_in = "消息" in body_text and ("订单" in body_text or "发闲置" in body_text)
        if not logged_in:
            return {
                    "success": False,
                    "has_quick_enter": quick_enter,
                    "message": "浏览器未处于已登录状态，未找到快速进入入口，需要重新扫码或密码登录",
                }

        browser_cookies = context.cookies()
        if not browser_cookies:
            return {"success": False, "has_quick_enter": quick_enter, "message": "浏览器未返回 Cookie"}
        new_cookie = _merge_browser_cookies(cookie_value, browser_cookies)
        return {
            "success": bool(new_cookie),
            "has_quick_enter": quick_enter,
            "new_cookies_str": new_cookie,
            "message": f"浏览器续期成功，获取到 {len(browser_cookies)} 个 Cookie",
        }
    except Exception as exc:
        return {"success": False, "message": f"浏览器续期异常：{str(exc)[:500]}"}
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass


def _find_login_frame(page):
    """返回闲鱼登录表单所在的页面或 iframe。"""
    for frame in [page, *page.frames]:
        try:
            if frame.locator("#fm-login-id").count() > 0 and frame.locator("#fm-login-password").count() > 0:
                return frame
        except Exception:
            continue
    return None


def _cookie_map(context) -> dict[str, str]:
    return {
        str(item.get("name")): str(item.get("value"))
        for item in context.cookies()
        if item.get("name") and item.get("value")
    }


def _sync_password_login(
    cookie_value: str,
    account_id: str,
    username: str,
    password: str,
    show_browser: bool = False,
) -> dict[str, Any]:
    """在 WebSocket 进程内执行一次密码登录。

    这是旧版自动续期的最后兜底：接口和已登录浏览器都不能恢复时，
    使用账号列表中保存的账号密码重新建立登录态。滑块/人脸等需要人工
    完成的验证会返回 verification_required，而不会伪造登录成功。
    """
    if sync_playwright is None:
        return {"success": False, "message": "WebSocket 服务未安装 Playwright，无法执行密码登录"}
    if not str(username or "").strip() or not str(password or "").strip():
        return {"success": False, "message": "未配置账号或密码，无法自动登录"}

    base_dir = Path(os.getenv("BROWSER_DATA_DIR", "./browser_data"))
    user_dir = base_dir / f"user_{account_id or 'unknown'}"
    user_dir.mkdir(parents=True, exist_ok=True)
    playwright = None
    context = None
    try:
        playwright = sync_playwright().start()
        context = playwright.chromium.launch_persistent_context(
            str(user_dir),
            headless=os.getenv("BROWSER_HEADLESS", "true").lower() != "false",
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-notifications",
                "--lang=zh-CN",
            ],
            viewport={"width": 1280, "height": 720},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/139.0.0.0 Safari/537.36"
            ),
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        if cookie_value.strip():
            payloads = _cookie_payloads(cookie_value)
            if payloads:
                context.add_cookies(payloads)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.goofish.com/im", wait_until="domcontentloaded", timeout=60000)

        # 先复用持久化浏览器中的登录态；首次启动时页面和 iframe 都可能
        # 延迟加载，必须轮询真实元素状态，不能只依赖固定 sleep。
        login_frame = None
        quick_enter_attempted = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not quick_enter_attempted:
                for frame in [page, *page.frames]:
                    for selector in (
                        'button:has-text("快速进入")',
                        'button[type="submit"]:has-text("快速进入")',
                        '.fm-button:has-text("快速进入")',
                        '.fn-button:has-text("快速进入")',
                    ):
                        try:
                            locator = frame.locator(selector)
                            if locator.count() > 0 and locator.first.is_visible():
                                quick_enter_attempted = True
                                locator.first.click(timeout=5000)
                                page.wait_for_timeout(2500)
                                cookies = context.cookies()
                                if _cookie_map(context).get("unb"):
                                    return {
                                        "success": True,
                                        "new_cookies_str": _merge_browser_cookies(cookie_value, cookies),
                                        "method": "browser_quick_enter",
                                        "message": "已复用浏览器登录态完成续期",
                                    }
                                break
                        except Exception:
                            continue
                    if quick_enter_attempted:
                        break
            login_frame = _find_login_frame(page)
            if login_frame is not None:
                break
            page.wait_for_timeout(500)

        if login_frame is None:
            # 页面可能已经登录，也可能弹出了风控验证。
            body_text = (page.locator("body").text_content() or "").strip()
            if any(word in body_text for word in ("滑块", "人脸", "安全验证", "请完成验证")):
                return {"success": False, "status": "verification_required", "message": "登录触发闲鱼安全验证，请完成滑块或人脸验证"}
            return {"success": False, "message": "未找到闲鱼密码登录表单，可能需要扫码或完成安全验证"}

        # 淘宝/闲鱼登录页通常先展示扫码页，需要切换到密码登录。
        try:
            password_tab = login_frame.locator("a.password-login-tab-item")
            if password_tab.count() > 0 and password_tab.first.is_visible():
                password_tab.first.click(timeout=5000)
        except Exception:
            pass

        password_input = login_frame.locator("#fm-login-password")
        try:
            password_input.wait_for(state="visible", timeout=15000)
            login_frame.locator("#fm-login-id").wait_for(state="visible", timeout=15000)
        except Exception:
            return {"success": False, "status": "transient_error", "message": "闲鱼密码登录表单加载超时，请重试"}
        login_frame.locator("#fm-login-id").fill(str(username).strip())
        password_input.fill(str(password))
        submit = None
        for selector in ("button.password-login", 'button[type="submit"]', ".fm-submit"):
            try:
                candidate = login_frame.locator(selector)
                if candidate.count() > 0:
                    candidate.first.wait_for(state="visible", timeout=5000)
                    submit = candidate.first
                    break
            except Exception:
                continue
        if submit is None:
            return {"success": False, "status": "transient_error", "message": "闲鱼密码登录按钮加载超时，请重试"}
        submit.click(timeout=5000)

        # 提交后等待真实登录结果。首次冷启动可能需要更长时间，不能在
        # 固定 5 秒后立即把仍在提交中的页面当成密码错误。
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            body_text = (page.locator("body").text_content() or "").strip()
            if any(word in body_text for word in ("滑块", "人脸", "安全验证", "请完成验证")):
                return {"success": False, "status": "verification_required", "message": "登录触发闲鱼安全验证，请完成滑块或人脸验证"}
            cookies = context.cookies()
            cookie_map = _cookie_map(context)
            form_visible = False
            try:
                form_visible = password_input.is_visible()
            except Exception:
                pass
            if cookie_map.get("unb") and not form_visible:
                return {
                    "success": True,
                    "new_cookies_str": _merge_browser_cookies(cookie_value, cookies),
                    "method": "password",
                    "message": "账号密码登录成功",
                }
            for selector in (".login-error-msg", ".fm-error", ".error-msg"):
                try:
                    error_text = (login_frame.locator(selector).first.text_content() or "").strip()
                    if error_text and login_frame.locator(selector).first.is_visible():
                        return {"success": False, "message": f"密码登录失败：{error_text[:300]}"}
                except Exception:
                    continue
            page.wait_for_timeout(1000)

        # 优先返回页面原始错误，便于界面告诉用户是密码错误还是被平台拦截。
        for selector in (".login-error-msg", ".fm-error", ".error-msg"):
            try:
                error_text = (login_frame.locator(selector).first.text_content() or "").strip()
                if error_text:
                    return {"success": False, "message": f"密码登录失败：{error_text[:300]}"}
            except Exception:
                continue
        return {"success": False, "message": "密码登录超时，未拿到有效 Cookie"}
    except Exception as exc:
        return {"success": False, "message": f"密码登录异常：{str(exc)[:500]}"}
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
            except Exception:
                pass


async def renew_browser_cookies(cookie_value: str, account_id: str) -> dict[str, Any]:
    """按账号串行执行浏览器续期，防止同一持久化目录被并发占用。"""
    lock = _account_locks[str(account_id)]
    async with lock:
        return await asyncio.to_thread(_sync_renew, cookie_value, str(account_id))


async def renew_password_cookies(
    cookie_value: str,
    account_id: str,
    username: str,
    password: str,
    show_browser: bool = False,
) -> dict[str, Any]:
    """按账号串行执行密码登录，复用 Cookie 续期的浏览器锁。"""
    lock = _account_locks[str(account_id)]
    async with lock:
        return await asyncio.to_thread(
            _sync_password_login,
            cookie_value,
            str(account_id),
            username,
            password,
            show_browser,
        )


__all__ = ["renew_browser_cookies", "renew_password_cookies"]
