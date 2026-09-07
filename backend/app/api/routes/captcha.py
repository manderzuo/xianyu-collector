# -*- coding: utf-8 -*-
"""登录、找回密码及风控验证码接口。

图形验证码在后端生成并一次性消费；邮箱验证码只有 SMTP 投递成功后才会
写入待验证状态。远程过滑块接口只转发到已配置的服务，不返回本地伪成功。
"""
from __future__ import annotations

import base64
import io
import random
import re
import secrets
import string
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import error, ok
from backend.app.services.email_service import send_verification_code_email
from common.config import settings
from common.db.session import get_session
from common.models import User
from common.models.system import SystemSetting

router = APIRouter(prefix="/api/v1/captcha", tags=["验证码"])

CAPTCHA_TTL = 300
EMAIL_CODE_TTL = 300
EMAIL_COOLDOWN = 60
MAX_EMAIL_ATTEMPTS = 5

_captcha_store: dict[str, dict[str, Any]] = {}
_email_code_store: dict[str, dict[str, Any]] = {}

REMOTE_KEYS = {
    "url": "captcha.remote_service_url",
    "secret_key": "captcha.remote_secret_key",
    "pass_cookies": "captcha.remote_pass_cookies",
    "block_remote_calls": "captcha.block_remote_calls",
    "local_weight": "captcha.real_mouse_weight_local",
    "remote_weight": "captcha.real_mouse_weight_remote",
    "remote_processing_max": "captcha.remote_processing_max",
    "remote_cooldown_seconds": "captcha.remote_cooldown_seconds",
}


class CaptchaRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=128)


class VerifyCaptchaRequest(CaptchaRequest):
    captcha_code: str = Field(min_length=4, max_length=8)


class SendEmailCodeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    type: str = Field(default="login", pattern="^(login|reset_password)$")


class VerifyEmailCodeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=128)
    code: str = Field(min_length=4, max_length=8)
    code_type: str = Field(default="login", pattern="^(login|reset_password)$")


class SliderSolveRequest(BaseModel):
    secret_key: str = ""
    account_id: str = "external"
    url: str = ""
    browser_timeout: int = Field(default=40, ge=20, le=120)
    cookies: str = ""
    device_id: str = ""


class RemoteConfigUpdate(BaseModel):
    url: str = ""
    secret_key: str = ""
    pass_cookies: bool = False
    block_remote_calls: bool = True
    local_weight: float = Field(default=1, ge=0)
    remote_weight: float = Field(default=1, ge=0)
    remote_processing_max: int = Field(default=0, ge=0)
    remote_cooldown_seconds: int = Field(default=0, ge=0)


class RemoteSliderTestRequest(BaseModel):
    url: str = ""
    secret_key: str = ""


def _cleanup() -> None:
    now = time.time()
    for store in (_captcha_store, _email_code_store):
        for key, item in list(store.items()):
            if float(item.get("expires_at", 0)) <= now:
                store.pop(key, None)


def _captcha_text(length: int = 4) -> str:
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _captcha_image(value: str) -> str:
    try:
        from PIL import Image, ImageDraw, ImageFont

        width, height = 120, 40
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        font = None
        for path in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ):
            try:
                font = ImageFont.truetype(path, 28)
                break
            except Exception:
                continue
        font = font or ImageFont.load_default()
        for _ in range(5):
            draw.line(
                (random.randrange(width), random.randrange(height), random.randrange(width), random.randrange(height)),
                fill=tuple(random.randrange(60, 200) for _ in range(3)),
                width=1,
            )
        for _ in range(45):
            draw.point(
                (random.randrange(width), random.randrange(height)),
                fill=tuple(random.randrange(40, 200) for _ in range(3)),
            )
        for index, char in enumerate(value):
            draw.text((10 + index * 25 + random.randrange(-2, 3), random.randrange(2, 9)), char, font=font, fill=(30, 30, 30))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:
        return ""


def _valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()))


def _verify_captcha(session_id: str) -> tuple[bool, str]:
    _cleanup()
    item = _captcha_store.get(session_id)
    if not item:
        return False, "图形验证码不存在或已过期"
    if not item.get("verified"):
        return False, "请先完成图形验证码"
    return True, "验证成功"


def consume_captcha(session_id: str) -> tuple[bool, str]:
    """校验并消费一次已通过的图形验证码，供注册等业务接口使用。"""
    valid, message = _verify_captcha(session_id)
    if valid:
        _captcha_store.pop(session_id, None)
    return valid, message


def check_email_code(email: str, code: str, code_type: str = "login") -> tuple[bool, str]:
    """供鉴权路由同步调用；校验成功会消费验证码。"""
    _cleanup()
    key = email.strip().lower()
    item = _email_code_store.get(key)
    if not item:
        return False, "验证码不存在或已过期"
    if item.get("type") != code_type:
        return False, "验证码类型不匹配"
    if str(item.get("code")) != str(code).strip():
        item["attempts"] = int(item.get("attempts", 0)) + 1
        if item["attempts"] >= MAX_EMAIL_ATTEMPTS:
            _email_code_store.pop(key, None)
            return False, "验证码错误次数过多，请重新获取验证码"
        return False, "验证码错误"
    _email_code_store.pop(key, None)
    return True, "验证码验证成功"


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


async def _setting_map(db: AsyncSession) -> dict[str, str]:
    rows = (await db.execute(select(SystemSetting))).scalars().all()
    return {str(row.setting_key): str(row.setting_value or "") for row in rows}


async def _save_setting(db: AsyncSession, key: str, value: str, *, secret: bool = False) -> None:
    item = (await db.execute(select(SystemSetting).where(SystemSetting.setting_key == key).limit(1))).scalar_one_or_none()
    if item is None:
        db.add(SystemSetting(setting_key=key, setting_value=value, is_secret=1 if secret else 0))
    else:
        item.setting_value = value
        if secret:
            item.is_secret = 1


@router.post("/generate")
async def generate_captcha(request: CaptchaRequest):
    _cleanup()
    value = _captcha_text()
    image = _captcha_image(value)
    if not image:
        return error("图形验证码生成失败", code="captcha_generation_failed")
    _captcha_store[request.session_id] = {"code": value, "verified": False, "expires_at": time.time() + CAPTCHA_TTL}
    return ok({"captcha_image": image, "session_id": request.session_id}, "图形验证码生成成功")


@router.post("/verify")
async def verify_captcha(request: VerifyCaptchaRequest):
    _cleanup()
    item = _captcha_store.get(request.session_id)
    if not item:
        return error("验证码不存在或已过期", code="captcha_expired")
    if str(item.get("code", "")).upper() != request.captcha_code.strip().upper():
        return error("验证码错误", code="captcha_invalid")
    item["verified"] = True
    return ok({"verified": True}, "验证码验证成功")


@router.post("/send-email-code")
async def send_email_code(request: SendEmailCodeRequest, db: AsyncSession = Depends(get_session)):
    email = request.email.strip().lower()
    if not _valid_email(email):
        return error("邮箱格式不正确", code="invalid_email")
    if request.session_id:
        valid, message = _verify_captcha(request.session_id)
        if not valid:
            return error(message, code="captcha_required")
    user = (await db.execute(select(User).where(User.email == email).limit(1))).scalar_one_or_none()
    if request.type in {"login", "reset_password"} and user is None:
        return error("该邮箱未注册", code="email_not_found")
    _cleanup()
    previous = _email_code_store.get(email)
    if previous and float(previous.get("sent_at", 0)) + EMAIL_COOLDOWN > time.time():
        return error("验证码发送过于频繁，请稍后再试", code="email_code_rate_limited")
    code = "".join(secrets.choice(string.digits) for _ in range(6))
    try:
        await send_verification_code_email(db, to_email=email, code=code, code_type=request.type)
    except Exception as exc:
        return error(str(exc)[:300], code="email_send_failed")
    _email_code_store[email] = {
        "code": code,
        "type": request.type,
        "sent_at": time.time(),
        "expires_at": time.time() + EMAIL_CODE_TTL,
        "attempts": 0,
    }
    return ok({"email": email, "expires_in": EMAIL_CODE_TTL}, "验证码已发送到您的邮箱，请查收")


@router.post("/verify-email-code")
async def verify_email_code(request: VerifyEmailCodeRequest):
    success, message = check_email_code(request.email, request.code, request.code_type)
    return ok(None, message) if success else error(message, code="email_code_invalid")


def _url_is_valid(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


@router.get("/remote-config")
async def get_remote_config(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    if not _is_admin(user):
        return error("仅管理员可以读取远程过滑块配置", code="forbidden")
    values = await _setting_map(db)
    return ok({
        "url": values.get(REMOTE_KEYS["url"], ""),
        "secret_key": values.get(REMOTE_KEYS["secret_key"], ""),
        "pass_cookies": values.get(REMOTE_KEYS["pass_cookies"], "false").lower() == "true",
        "block_remote_calls": values.get(REMOTE_KEYS["block_remote_calls"], "true").lower() == "true",
        "local_weight": float(values.get(REMOTE_KEYS["local_weight"], "1") or 1),
        "remote_weight": float(values.get(REMOTE_KEYS["remote_weight"], "1") or 1),
        "remote_processing_max": int(values.get(REMOTE_KEYS["remote_processing_max"], "0") or 0),
        "remote_cooldown_seconds": int(values.get(REMOTE_KEYS["remote_cooldown_seconds"], "0") or 0),
    }, "查询成功")


@router.put("/remote-config")
async def update_remote_config(request: RemoteConfigUpdate, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    if not _is_admin(user):
        return error("仅管理员可以修改远程过滑块配置", code="forbidden")
    if request.url and not _url_is_valid(request.url):
        return error("远程服务URL必须以 http:// 或 https:// 开头", code="invalid_remote_url")
    if "api.xianyusite.shop" in request.url.lower() or "api.zhinianblog.cn" in request.url.lower():
        return error("该URL是Token获取接口，不是远程过滑块服务", code="wrong_service_url")
    values = {
        REMOTE_KEYS["url"]: request.url.strip(),
        REMOTE_KEYS["secret_key"]: request.secret_key.strip(),
        REMOTE_KEYS["pass_cookies"]: str(request.pass_cookies).lower(),
        REMOTE_KEYS["block_remote_calls"]: str(request.block_remote_calls).lower(),
        REMOTE_KEYS["local_weight"]: str(request.local_weight),
        REMOTE_KEYS["remote_weight"]: str(request.remote_weight),
        REMOTE_KEYS["remote_processing_max"]: str(request.remote_processing_max),
        REMOTE_KEYS["remote_cooldown_seconds"]: str(request.remote_cooldown_seconds),
    }
    for key, value in values.items():
        await _save_setting(db, key, value, secret=key == REMOTE_KEYS["secret_key"])
    await db.commit()
    return ok({"saved": True}, "远程过滑块配置已保存")


@router.post("/slider-solve/test")
async def test_remote_slider(request: RemoteSliderTestRequest, user=Depends(get_current_user)):
    if not _is_admin(user):
        return error("仅管理员可以测试远程过滑块服务", code="forbidden")
    if not _url_is_valid(request.url):
        return error("请先填写有效的远程服务URL", code="invalid_remote_url")
    payload = {"secret_key": request.secret_key.strip(), "account_id": "connectivity-test", "url": ""}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(request.url.strip(), json=payload)
        if response.status_code >= 500:
            return error(f"远程服务返回异常（HTTP {response.status_code}）", code="remote_unavailable")
        return ok({"http_status": response.status_code}, f"远程服务已连通（HTTP {response.status_code}）")
    except Exception as exc:
        return error(f"无法连接远程服务：{str(exc)[:240]}", code="remote_unavailable")


@router.post("/slider-solve")
async def slider_solve(request: SliderSolveRequest, db: AsyncSession = Depends(get_session)):
    values = await _setting_map(db)
    if values.get(REMOTE_KEYS["block_remote_calls"], "true").lower() == "true":
        return error("系统已禁止远程过滑块调用", code="remote_calls_blocked")
    remote_url = values.get(REMOTE_KEYS["url"], "").strip()
    if not _url_is_valid(remote_url):
        return error("远程过滑块服务尚未配置", code="not_configured")
    configured_secret = values.get(REMOTE_KEYS["secret_key"], "")
    if not request.secret_key or request.secret_key != configured_secret:
        return error("远程过滑块秘钥无效", code="invalid_secret")
    if not request.url.strip():
        return error("punish链接不能为空", code="invalid_url")
    payload = {
        "secret_key": request.secret_key,
        "account_id": request.account_id,
        "url": request.url,
        "browser_timeout": request.browser_timeout,
        "cookies": request.cookies if values.get(REMOTE_KEYS["pass_cookies"], "false").lower() == "true" else "",
        "device_id": request.device_id,
    }
    try:
        async with httpx.AsyncClient(timeout=request.browser_timeout + 10, follow_redirects=False) as client:
            response = await client.post(remote_url, json=payload)
            body = response.json()
        if response.status_code >= 400:
            return error(f"远程过滑块失败（HTTP {response.status_code}）", code="remote_failed", data=body)
        if isinstance(body, dict) and body.get("success") is False:
            return error(str(body.get("message") or "远程过滑块失败"), code="remote_failed", data=body.get("data"))
        return ok(body.get("data") if isinstance(body, dict) else body, "过滑块成功")
    except Exception as exc:
        return error(f"远程过滑块请求失败：{str(exc)[:240]}", code="remote_failed")
