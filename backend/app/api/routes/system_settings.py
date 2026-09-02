# -*- coding: utf-8 -*-
"""系统设置中的特殊键接口。

普通设置仍由资源路由提供；带点号的旧版设置键（例如
``token.remote_url``）需要按键名更新，而不是被兼容兜底层当成一条普通记录。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.core.response import error
from backend.app.services.email_service import send_email
from common.db.session import get_session
from common.models.system import SystemSetting
from common.services.remote_token_api import (
    is_empty_cookie_validation_message,
    request_remote_xianyu_token,
    validate_remote_token_settings,
)

router = APIRouter(prefix="/api/v1/system-settings", tags=["系统设置"])


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


@router.get("")
@router.get("/")
async def list_settings(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """读取真实系统设置，避免被旧版兼容兜底层返回空记录。"""
    admin = _is_admin(user)
    rows = (
        await db.execute(select(SystemSetting).order_by(SystemSetting.id.asc()))
    ).scalars().all()
    return ok(
        {
            "items": [
                {
                    "id": item.id,
                    "setting_key": item.setting_key,
                    "setting_value": item.setting_value if admin or not item.is_secret else "***",
                    "is_secret": item.is_secret,
                    "updated_at": item.updated_at.isoformat() if item.updated_at else None,
                }
                for item in rows
            ],
            "total": len(rows),
            "page": 1,
            "page_size": len(rows),
        },
        "查询成功",
    )


async def _upsert_setting(
    key: str,
    value: str,
    db: AsyncSession,
) -> SystemSetting:
    item = (
        await db.execute(select(SystemSetting).where(SystemSetting.setting_key == key).limit(1))
    ).scalar_one_or_none()
    if item is None:
        item = SystemSetting(
            setting_key=key,
            setting_value=value,
            is_secret=1 if key == "token.remote_secret_key" else 0,
        )
        db.add(item)
    else:
        item.setting_value = value
        if key == "token.remote_secret_key":
            item.is_secret = 1
    await db.commit()
    await db.refresh(item)
    return item


@router.put("/{key:path}")
async def update_setting(
    key: str,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可以修改系统设置")
    clean_key = key.strip().strip("/")
    if not clean_key or "/" in clean_key:
        raise HTTPException(status_code=422, detail="系统设置键无效")
    value = str((payload or {}).get("value") or "").strip()
    if clean_key == "token.api_mode" and value not in {"web", "remote"}:
        raise HTTPException(status_code=422, detail="Token获取方式仅支持网页接口或远程接口")
    if clean_key == "token.remote_url":
        current_secret = (
            await db.execute(
                select(SystemSetting.setting_value)
                .where(SystemSetting.setting_key == "token.remote_secret_key")
                .limit(1)
            )
        ).scalar_one_or_none()
        validation_error = validate_remote_token_settings(value, str(current_secret or "dummy"))
        if validation_error and value:
            # URL 单独保存时秘钥可能还未保存，保留专门的 URL 格式校验。
            from urllib.parse import urlparse
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise HTTPException(status_code=422, detail=validation_error)
    item = await _upsert_setting(clean_key, value, db)
    return ok(
        {"setting_key": item.setting_key, "setting_value": item.setting_value},
        "系统设置已保存",
    )


@router.post("/test-token-remote")
async def test_token_remote(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
):
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可以测试系统设置")
    values = payload or {}
    url = str(values.get("remote_url") or "").strip()
    secret_key = str(values.get("remote_secret_key") or "").strip()
    validation_error = validate_remote_token_settings(url, secret_key)
    if validation_error:
        raise HTTPException(status_code=422, detail=validation_error)

    # 测试不发送账号 Cookie，只确认 URL 与秘钥是否能被远端识别。
    result = await request_remote_xianyu_token(url, secret_key, cookies="", timeout_seconds=10)
    if result.success:
        return ok(
            {"token": result.token, "device_id": result.device_id, "api_mode": result.api_mode or "remote"},
            "远程接口测试成功",
        )
    if is_empty_cookie_validation_message(result.message):
        return ok(
            {"token": "", "device_id": "", "api_mode": "remote"},
            "远程接口已连通，服务已识别秘钥；正式取Token时会发送账号Cookie",
        )
    return {"success": False, "code": "remote_token_failed", "message": result.message, "data": None}


@router.post("/test-email")
async def test_email(
    email: str,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """发送一封真实测试邮件，只有 SMTP 投递成功才返回 success=true。"""
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可以测试邮件配置")
    recipient = email.strip()
    if not recipient:
        return error("请填写收件邮箱", code="invalid_email")
    try:
        await send_email(
            db,
            to_email=recipient,
            subject="系统邮件配置测试",
            content="这是一封系统邮件配置测试消息。收到此邮件表示 SMTP 配置已生效。",
        )
    except Exception as exc:
        return error(str(exc)[:300], code="email_send_failed")
    return ok({"sent": True, "email": recipient}, "测试邮件已发送")
