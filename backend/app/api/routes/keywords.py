# -*- coding: utf-8 -*-
"""按闲鱼账号维护关键词规则的兼容协议实现。"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from common.config import settings
from common.db.session import get_session
from common.models.accounts import Account
from common.models import FeatureRecord

router = APIRouter(prefix="/api/v1/keywords-with-item-id", tags=["关键词规则"])
FEATURE = "keywords-with-item-id"


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _serialize(item: FeatureRecord) -> dict[str, Any]:
    data = dict(item.payload or {})
    data.update({"id": str(item.id), "created_at": item.created_at, "updated_at": item.updated_at})
    return data


async def _account_rows(account_id: str | None, user: dict[str, Any], db: AsyncSession):
    statement = select(FeatureRecord).where(FeatureRecord.feature == FEATURE, FeatureRecord.owner_id == _uid(user))
    rows = (await db.execute(statement.order_by(FeatureRecord.id.desc()))).scalars().all()
    if account_id is None:
        return rows
    return [row for row in rows if str((row.payload or {}).get("account_id")) == str(account_id)]


@router.get("")
@router.get("/")
async def list_all_keywords(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = await _account_rows(None, user, db)
    items = [_serialize(row) for row in rows]
    return ok({"items": items, "list": items, "total": len(items)}, "关键词查询成功")


@router.get("/{account_id}")
async def list_keywords(account_id: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = await _account_rows(account_id, user, db)
    items = [_serialize(row) for row in rows]
    return ok({"items": items, "list": items, "total": len(items)}, "关键词查询成功")


@router.post("/{account_id}")
async def save_keywords(account_id: str, payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    values = payload or {}
    keywords = values.get("keywords")
    if not isinstance(keywords, list):
        raise HTTPException(status_code=422, detail="keywords 必须是数组")
    old_rows = await _account_rows(account_id, user, db)
    # 前端保存文本规则时会刻意过滤图片规则；只替换文本规则，
    # 否则新增/编辑文本关键词会把该账号已有的图片关键词一并删除。
    old_text_rows = [row for row in old_rows if str((row.payload or {}).get("type") or "text") != "image"]
    for old_row in old_text_rows:
        await db.delete(old_row)
    created = []
    for value in keywords:
        if not isinstance(value, dict) or not str(value.get("keyword") or "").strip():
            continue
        item = FeatureRecord(owner_id=_uid(user), feature=FEATURE, external_id=uuid4().hex, status="active", payload={"account_id": str(account_id), "keyword": str(value.get("keyword") or ""), "reply": str(value.get("reply") or ""), "item_id": str(value.get("item_id") or ""), "type": str(value.get("type") or "text")})
        db.add(item)
        created.append(item)
    await db.commit()
    # AsyncSession 提交后会使 ORM 实例过期，直接读取 created_at/updated_at
    # 会触发同步式懒加载并抛出 MissingGreenlet。显式刷新后再返回，避免
    # “数据库已写入但前端显示保存失败”的假失败。
    for item in created:
        await db.refresh(item)
    return ok({"count": len(created), "items": [_serialize(item) for item in created]}, "关键词已保存")


@router.put("/{account_id}/{keyword}")
async def update_keyword(account_id: str, keyword: str, old_item_id: str | None = Query(None), payload: dict[str, Any] | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = await _account_rows(account_id, user, db)
    decoded = keyword
    item = next((row for row in rows if str((row.payload or {}).get("keyword")) == decoded and (old_item_id is None or str((row.payload or {}).get("item_id") or "") == str(old_item_id))), None)
    if item is None:
        raise HTTPException(status_code=404, detail="关键词不存在")
    values = payload or {}
    target_account_id = str(values.get("account_id") or account_id).strip()
    if not target_account_id:
        raise HTTPException(status_code=422, detail="所属账号不能为空")
    if target_account_id != str(account_id):
        try:
            target_account_pk = int(target_account_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="所属账号无效") from exc
        target_account = (
            await db.execute(
                select(Account).where(
                    Account.id == target_account_pk,
                    Account.user_id == _uid(user),
                )
            )
        ).scalar_one_or_none()
        if target_account is None:
            raise HTTPException(status_code=404, detail="目标所属账号不存在或无权使用")

        new_keyword = str(values.get("keyword") or (item.payload or {}).get("keyword") or "").strip()
        new_item_id = str(values.get("item_id") or (item.payload or {}).get("item_id") or "").strip()
        target_rows = await _account_rows(target_account_id, user, db)
        duplicate = next(
            (
                row for row in target_rows
                if row.id != item.id
                and str((row.payload or {}).get("keyword") or "").strip() == new_keyword
                and str((row.payload or {}).get("item_id") or "").strip() == new_item_id
            ),
            None,
        )
        if duplicate is not None:
            raise HTTPException(status_code=409, detail="目标账号已存在相同商品范围的关键词")

    data = dict(item.payload or {})
    data.update({key: value for key, value in values.items() if key in {"keyword", "reply", "item_id", "type"}})
    # 编辑时切换所属账号必须迁移规则，而不是只返回成功但继续留在原账号。
    data["account_id"] = target_account_id
    item.payload = data
    await db.commit()
    await db.refresh(item)
    return ok(_serialize(item), "关键词已更新")


@router.delete("/{account_id}/{keyword}")
async def delete_keyword(account_id: str, keyword: str, item_id: str | None = Query(None), rule_id: str | None = Query(None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = await _account_rows(account_id, user, db)
    item = next((row for row in rows if (rule_id and str(row.id) == str(rule_id)) or (str((row.payload or {}).get("keyword")) == keyword and (item_id is None or str((row.payload or {}).get("item_id") or "") == str(item_id)))), None)
    if item is None:
        raise HTTPException(status_code=404, detail="关键词不存在")
    deleted_id = item.id
    await db.delete(item)
    await db.commit()
    return ok({"id": deleted_id, "deleted": True}, "关键词已删除")


@router.get("/{account_id}/export")
async def export_keywords(account_id: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    rows = await _account_rows(account_id, user, db)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=["keyword", "reply", "item_id", "type"])
    writer.writeheader()
    for row in rows:
        data = row.payload or {}
        writer.writerow({key: data.get(key, "") for key in ("keyword", "reply", "item_id", "type")})
    content = ("\ufeff" + buffer.getvalue()).encode("utf-8")
    return StreamingResponse(io.BytesIO(content), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="keywords_{account_id}.csv"'})


@router.post("/{account_id}/image")
async def add_image_keyword(
    account_id: str,
    keyword: str = Form(...),
    image: UploadFile = File(...),
    item_id: str | None = Form(None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    upload_dir = Path(settings.static_dir) / "uploads" / "keywords"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(image.filename or "keyword-image.bin").name
    target = upload_dir / f"{uuid4().hex}_{safe_name}"
    target.write_bytes(await image.read())
    image_url = f"/static/uploads/keywords/{target.name}"
    item = FeatureRecord(
        owner_id=_uid(user), feature=FEATURE, external_id=uuid4().hex, status="active",
        payload={"account_id": str(account_id), "keyword": keyword.strip(), "reply": "", "item_id": item_id or "", "type": "image", "image_url": image_url},
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return ok({"keyword": keyword.strip(), "image_url": image_url, "item_id": item_id or "", "id": str(item.id)}, "图片关键词已添加")


@router.post("/{account_id}/import")
async def import_keywords(account_id: str, file: UploadFile = File(...), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="导入文件必须是 UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text))
    existing = {str((row.payload or {}).get("keyword")): row for row in await _account_rows(account_id, user, db)}
    added = 0
    updated = 0
    for value in reader:
        keyword = str(value.get("keyword") or value.get("关键词") or "").strip()
        if not keyword:
            continue
        payload = {"account_id": str(account_id), "keyword": keyword, "reply": str(value.get("reply") or value.get("回复") or ""), "item_id": str(value.get("item_id") or value.get("商品ID") or ""), "type": "text"}
        item = existing.get(keyword)
        if item is None:
            db.add(FeatureRecord(owner_id=_uid(user), feature=FEATURE, external_id=uuid4().hex, status="active", payload=payload))
            added += 1
        else:
            item.payload = {**(item.payload or {}), **payload}
            updated += 1
    await db.commit()
    return ok({"added": added, "updated": updated}, "关键词导入成功")
