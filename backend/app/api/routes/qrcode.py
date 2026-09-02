# -*- coding: utf-8 -*-
"""群二维码的公开读取和管理员上传接口。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import error, ok
from common.config import settings

router = APIRouter(prefix="/api/v1/qrcode", tags=["群二维码"])
_ALLOWED_TYPES = {"wechat", "qq", "wechat_official", "telegram", "reward"}
_ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_MAX_SIZE = 2 * 1024 * 1024


def _root() -> Path:
    root = Path(settings.static_dir) / "qrcode"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _prefix(qrcode_type: str) -> str:
    return qrcode_type.replace("_", "-")


def _admin(user: dict) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


@router.get("/{qrcode_type}")
async def get_qrcode(qrcode_type: str):
    if qrcode_type not in _ALLOWED_TYPES:
        return error("无效的二维码类型", code="invalid_qrcode_type")
    prefix = _prefix(qrcode_type)
    for extension in _ALLOWED_EXTENSIONS:
        path = _root() / f"{prefix}-group{extension}"
        if path.is_file():
            return ok({"image_url": f"/static/qrcode/{path.name}"}, "查询成功")
    return error("二维码未配置", code="qrcode_not_configured")


@router.post("/{qrcode_type}")
async def upload_qrcode(
    qrcode_type: str,
    image: UploadFile = File(...),
    user=Depends(get_current_user),
):
    if not _admin(user):
        return error("仅管理员可以上传群二维码", code="forbidden")
    if qrcode_type not in _ALLOWED_TYPES:
        return error("无效的二维码类型，只支持 wechat、qq、wechat_official、telegram 或 reward", code="invalid_qrcode_type")
    content_type = str(image.content_type or "").lower()
    if not content_type.startswith("image/"):
        return error("请上传图片文件", code="invalid_image")
    content = await image.read(_MAX_SIZE + 1)
    if len(content) > _MAX_SIZE:
        return error("图片大小不能超过2MB", code="image_too_large")
    if not content:
        return error("图片文件为空", code="invalid_image")
    extension = Path(image.filename or "").suffix.lower()
    if extension not in _ALLOWED_EXTENSIONS:
        extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}.get(content_type, ".png")
    root = _root()
    prefix = _prefix(qrcode_type)
    for old_file in root.glob(f"{prefix}-group.*"):
        if old_file.is_file():
            old_file.unlink()
    target = root / f"{prefix}-group{extension}"
    try:
        target.write_bytes(content)
    except OSError as exc:
        return error(f"二维码保存失败：{str(exc)[:200]}", code="qrcode_upload_failed")
    return ok({"image_url": f"/static/qrcode/{target.name}"}, "上传成功")
