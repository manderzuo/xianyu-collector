# -*- coding: utf-8 -*-
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Depends, File, UploadFile, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from common.config import settings
from common.db.session import get_session
from common.models import Upload
from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok

router = APIRouter(prefix="/api/v1/upload", tags=["文件上传"], dependencies=[Depends(get_current_user)])


@router.post("")
@router.post("/upload-image")
async def upload(file: UploadFile = File(...), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    upload_dir = Path(settings.static_dir) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "upload.bin").name
    target = upload_dir / f"{uuid4().hex}_{safe_name}"
    content = await file.read()
    target.write_bytes(content)
    item = Upload(owner_id=int(user.get("sub", 1)), filename=safe_name, path=str(target), mime_type=file.content_type, size=len(content))
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return ok({"id": item.id, "filename": safe_name, "size": len(content), "url": f"/static/uploads/{target.name}"}, "文件已上传")


@router.get("")
async def list_uploads(user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Upload).where(Upload.owner_id == int(user.get("sub", 1))).order_by(Upload.id.desc()).limit(100))
    items = [{"id": item.id, "filename": item.filename, "mime_type": item.mime_type, "size": item.size, "url": f"/static/uploads/{Path(item.path).name}", "created_at": item.created_at.isoformat() if item.created_at else None} for item in result.scalars().all()]
    return ok({"items": items, "total": len(items)})


@router.delete("/{upload_id}")
async def delete_upload(upload_id: int, user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    item = (await session.execute(select(Upload).where(Upload.id == upload_id, Upload.owner_id == int(user.get("sub", 1))))).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    target = Path(item.path)
    if target.exists():
        target.unlink()
    await session.delete(item)
    await session.commit()
    return ok({"id": upload_id, "deleted": True}, "文件已删除")
