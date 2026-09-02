"""商品发布媒体上传接口。"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, UploadFile

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import error, ok
from common.config import settings


router = APIRouter(prefix="/api/v1/product-publish", tags=["商品发布"])

_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_VIDEO_MAX_BYTES = 100 * 1024 * 1024


def _upload_dir() -> Path:
    directory = Path(settings.static_dir) / "uploads" / "products"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _safe_filename(filename: str | None, fallback: str) -> str:
    name = Path(filename or fallback).name.strip()
    return name or fallback


async def _save_file(file: UploadFile, max_bytes: int, media_label: str) -> tuple[Path, int] | str:
    content = await file.read()
    size = len(content)
    if size == 0:
        return f"文件 {file.filename or ''} 为空"
    if size > max_bytes:
        return f"文件 {file.filename or ''} 超过大小限制（{max_bytes // 1024 // 1024}MB）"

    safe_name = _safe_filename(file.filename, f"upload.{media_label}")
    content_type = (file.content_type or "").lower()
    # 部分桌面端 multipart 客户端会把文件标成 application/octet-stream，
    # 这类请求仍可依据文件扩展名上传；浏览器正常上传时则使用 image/*/video/*。
    generic_content_types = {"application/octet-stream", "application/binary"}
    if content_type and not content_type.startswith(f"{media_label}/") and content_type not in generic_content_types:
        return f"文件 {file.filename or ''} 不是有效的{media_label}文件"

    target = _upload_dir() / f"{uuid4().hex}_{safe_name}"
    try:
        target.write_bytes(content)
    except OSError as exc:
        return f"保存文件失败：{exc}"
    return target, size


@router.post("/upload/images")
async def upload_product_images(
    files: list[UploadFile] = File(...),
    user=Depends(get_current_user),
):
    """上传商品图片，返回发布器可读取的容器内绝对路径和预览 URL。"""
    del user
    if len(files) > 9:
        return error("最多上传9张图片", code="validation_error")

    saved: list[tuple[Path, int]] = []
    for file in files:
        result = await _save_file(file, _IMAGE_MAX_BYTES, "image")
        if isinstance(result, str):
            for path, _ in saved:
                path.unlink(missing_ok=True)
            return error(result, code="upload_failed")
        saved.append(result)

    return ok({
        "paths": [str(path) for path, _ in saved],
        "urls": [f"/static/uploads/products/{path.name}" for path, _ in saved],
    }, f"成功上传 {len(saved)} 张图片")


@router.post("/upload/videos")
async def upload_product_videos(
    files: list[UploadFile] = File(...),
    user=Depends(get_current_user),
):
    """上传商品视频，返回发布器可读取的容器内绝对路径和预览 URL。"""
    del user
    if len(files) > 3:
        return error("最多上传3个视频", code="validation_error")

    saved: list[tuple[Path, int, str]] = []
    for file in files:
        result = await _save_file(file, _VIDEO_MAX_BYTES, "video")
        if isinstance(result, str):
            for path, _, _ in saved:
                path.unlink(missing_ok=True)
            return error(result, code="upload_failed")
        path, size = result
        saved.append((path, size, file.filename or path.name))

    videos = [
        {
            "path": str(path),
            "url": f"/static/uploads/products/{path.name}",
            "name": name,
            "size": size,
        }
        for path, size, name in saved
    ]
    return ok({
        "videos": videos,
        "paths": [item["path"] for item in videos],
        "urls": [item["url"] for item in videos],
    }, f"成功上传 {len(videos)} 个视频")
