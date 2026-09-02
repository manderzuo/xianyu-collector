"""用户设置键值接口兼容层。

旧版前端按 ``/user-settings/{key}`` 读写单个设置。迁移后的通用资源接口
返回分页记录，且无法接受旧版的 ``{value, description}`` 请求体，因此这里
提供一个明确的键值接口，保证免责声明及个人设置可以真正持久化。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import error, ok
from backend.app.services.card_dock import create_card_secret_key
from common.config import settings
from common.db.session import get_session
from common.models import FeatureRecord

router = APIRouter(prefix="/api/v1", tags=["用户设置兼容"])


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _features(key: str) -> tuple[str, str]:
    return (f"user-setting:{key}", f"legacy:user-settings/{key}")


@router.post("/user-settings/card-secret-key/create")
async def create_distribution_card_secret_key(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """调用已配置的密钥管理服务创建分销卡券对接密钥。"""
    result = await create_card_secret_key(
        _uid(user),
        str(user.get("username") or user.get("nickname") or _uid(user)),
        db,
    )
    if not result.get("success"):
        return result
    return ok(result.get("data") or {}, str(result.get("message") or "对接卡密秘钥创建成功"))


@router.post("/user-settings/payment-qrcode/upload")
async def upload_payment_qrcode(
    file: UploadFile = File(...),
    payment_type: str = Form(...),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """保存用户收款码，并把收款方式一并写入个人设置。"""
    payment_type = str(payment_type or "").strip().lower()
    if payment_type not in {"alipay", "wechat"}:
        return error("收款方式必须是支付宝或微信", code="validation_error")

    content_type = str(file.content_type or "").lower()
    if content_type and not content_type.startswith("image/"):
        return error("收款码必须是图片文件", code="validation_error")
    content = await file.read(5 * 1024 * 1024 + 1)
    if not content:
        return error("收款码文件为空", code="validation_error")
    if len(content) > 5 * 1024 * 1024:
        return error("收款码不能超过5MB", code="validation_error")

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(content_type, ".png")
    upload_dir = Path(settings.static_dir) / "uploads" / "payment-qrcodes"
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / f"{uuid4().hex}{suffix}"
    try:
        target.write_bytes(content)
    except OSError as exc:
        return error(f"保存收款码失败：{exc}", code="upload_failed")

    user_id = _uid(user)
    image_url = f"/static/uploads/payment-qrcodes/{target.name}"
    for key, value in (("payment_qrcode", image_url), ("payment_type", payment_type)):
        item = await _find(key, user_id, db)
        if item is None:
            item = FeatureRecord(owner_id=user_id, feature=f"user-setting:{key}", external_id=key, status="active", payload={})
            db.add(item)
        item.payload = {**(item.payload or {}), "value": value}
    await db.commit()
    return ok({"image_url": image_url, "payment_type": payment_type}, "收款码上传成功")


async def _find(key: str, user_id: int, db: AsyncSession) -> FeatureRecord | None:
    return (
        await db.execute(
            select(FeatureRecord)
            .where(FeatureRecord.owner_id == user_id, FeatureRecord.feature.in_(_features(key)))
            .order_by(FeatureRecord.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@router.get("/user-settings/{key}")
async def get_user_setting(key: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = await _find(key, _uid(user), db)
    payload = dict(item.payload or {}) if item else {}
    return ok({"key": key, "value": payload.get("value"), "description": payload.get("description")}, "设置查询成功")


@router.put("/user-settings/{key}")
async def update_user_setting(
    key: str,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    user_id = _uid(user)
    item = await _find(key, user_id, db)
    values = payload or {}
    if item is None:
        item = FeatureRecord(owner_id=user_id, feature=f"user-setting:{key}", external_id=key, status="active", payload={})
        db.add(item)
    item.payload = {**(item.payload or {}), "value": values.get("value"), "description": values.get("description")}
    await db.commit()
    await db.refresh(item)
    return ok({"key": key, **dict(item.payload or {})}, "设置已保存")


@router.delete("/user-settings/{key}")
async def delete_user_setting(key: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    item = await _find(key, _uid(user), db)
    if item is not None:
        await db.delete(item)
        await db.commit()
    return ok({"key": key, "deleted": item is not None}, "设置已删除")
