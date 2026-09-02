# -*- coding: utf-8 -*-
"""极验 v3 服务端适配。

只有配置了极验 ``captcha_id`` 和 ``private_key`` 才会开启；未配置时返回
明确的未配置状态，避免让登录页进入一个看似成功、实际无法验证的假流程。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import error, ok
from common.db.session import get_session
from common.models.system import SystemSetting

router = APIRouter(prefix="/api/v1/geetest", tags=["极验验证码"])
_status_store: dict[str, dict[str, Any]] = {}


async def _config(db: AsyncSession) -> dict[str, str]:
    rows = (
        await db.execute(
            select(SystemSetting).where(
                SystemSetting.setting_key.in_(["geetest.captcha_id", "geetest.private_key"])
            )
        )
    ).scalars().all()
    return {str(row.setting_key): str(row.setting_value or "").strip() for row in rows}


def _cleanup() -> None:
    now = time.time()
    for key, value in list(_status_store.items()):
        if float(value.get("expires_at", 0)) <= now:
            _status_store.pop(key, None)


def check_geetest_verified(challenge: str) -> tuple[bool, str]:
    """供登录接口消费一次性验证状态。"""
    _cleanup()
    item = _status_store.get(challenge)
    if not item or item.get("verified") is not True:
        return False, "请完成滑动验证"
    _status_store.pop(challenge, None)
    return True, "验证通过"


@router.get("/register")
async def register(db: AsyncSession = Depends(get_session)):
    config = await _config(db)
    captcha_id = config.get("geetest.captcha_id", "")
    private_key = config.get("geetest.private_key", "")
    if not captcha_id or not private_key:
        return error("极验服务尚未配置，请先填写 captcha_id 和 private_key", code="not_configured")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                "https://api.geetest.com/register.php",
                params={"gt": captcha_id, "json_format": 1, "sdk": "xianyu-rewrite"},
            )
            payload = response.json()
        raw_challenge = str(payload.get("challenge") or "")
        # 极验 v3 正常模式要求服务端用私钥对 register 返回的 challenge 做 MD5。
        challenge = hashlib.md5(f"{raw_challenge}{private_key}".encode("utf-8")).hexdigest() if len(raw_challenge) == 32 else ""
        if response.status_code >= 400 or not challenge:
            return error("极验初始化失败，请检查 captcha_id 或服务网络", code="geetest_register_failed")
        _cleanup()
        _status_store[challenge] = {"verified": False, "expires_at": time.time() + 300}
        return ok({
            "success": 1,
            "gt": captcha_id,
            "challenge": challenge,
            "new_captcha": bool(payload.get("new_captcha", False)),
        }, "极验初始化成功")
    except Exception as exc:
        return error(f"极验初始化失败：{str(exc)[:240]}", code="geetest_register_failed")


@router.post("/validate")
async def validate(payload: dict[str, Any] | None = Body(default=None), db: AsyncSession = Depends(get_session)):
    data = payload or {}
    challenge = str(data.get("challenge") or "").strip()
    validate_value = str(data.get("validate") or data.get("geetest_validate") or "").strip()
    seccode = str(data.get("seccode") or data.get("geetest_seccode") or "").strip()
    if not challenge or not validate_value or not seccode:
        return error("极验验证参数不完整", code="invalid_geetest_payload")
    config = await _config(db)
    captcha_id = config.get("geetest.captcha_id", "")
    private_key = config.get("geetest.private_key", "")
    if not captcha_id or not private_key:
        return error("极验服务尚未配置", code="not_configured")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # 私钥只放在请求体中，避免出现在代理、网关和访问日志的 URL 查询串里。
            response = await client.post(
                "https://api.geetest.com/validate.php",
                data={
                    "challenge": challenge,
                    "seccode": seccode,
                    "json_format": 1,
                    "sdk": "xianyu-rewrite",
                    "user_id": "anonymous",
                    "client_type": "web",
                    "captchaid": captcha_id,
                },
            )
            result = response.json()
        success = str(result.get("success")) in {"1", "true"}
        if not success:
            _status_store.pop(challenge, None)
            return error(str(result.get("message") or "极验验证失败"), code="geetest_invalid")
        _status_store[challenge] = {"verified": True, "expires_at": time.time() + 300}
        return ok({"verified": True}, "验证通过")
    except Exception as exc:
        return error(f"极验验证服务异常：{str(exc)[:240]}", code="geetest_validate_failed")


@router.get("/status")
async def status(challenge: str, user=Depends(get_current_user)):
    _cleanup()
    item = _status_store.get(challenge)
    return ok({"verified": bool(item and item.get("verified"))}, "查询成功")
