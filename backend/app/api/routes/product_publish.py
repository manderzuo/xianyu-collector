"""商品发布真实接口。

该模块覆盖发布页最关键的三条链路：账号能力检测、单品发布和发布日志。
旧版兼容层仍保留其它历史路径，但不能再接管这些已具备专用实现的路径。
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import error, ok
from common.db.session import async_session_maker, get_session
from common.models import Account, AccountCookie, CapabilityCheck, FeatureRecord
from common.services.account_sync import sync_account_products
from common.services.amap_inputtips import AmapInputTipsError, search_input_tips
from common.services.goofish_publish import GoofishPublishError, detect_publish_capability, publish_item
from common.services.goofish_client import GoofishClient
from common.services.platform_category_service import CategoryRecommendationError, PlatformCategoryService


logger = logging.getLogger("xr.product_publish")
router = APIRouter(prefix="/api/v1/product-publish", tags=["商品发布"])


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


async def _owned_account(account_id: int, user: dict[str, Any], db: AsyncSession) -> Account:
    statement = select(Account).where(Account.id == account_id)
    if not _is_admin(user):
        statement = statement.where(Account.user_id == _uid(user))
    account = (await db.execute(statement)).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="发布账号不存在或无权使用")
    return account


async def _save_latest_cookie(account: Account, cookie: str, db: AsyncSession) -> None:
    normalized = cookie.strip()
    if not normalized or normalized == (account.cookie or "").strip():
        return
    account.cookie = normalized
    db.add(AccountCookie(account_id=account.id, cookie_value=normalized, status="active"))


async def _record_capability(
    account: Account,
    result: dict[str, Any],
    db: AsyncSession,
) -> None:
    check = CapabilityCheck(
        owner_id=int(account.user_id),
        account_id=account.id,
        capability="publish",
        status="available" if result.get("success") else "unavailable",
        detail=str(result.get("message") or "账号发布能力检测失败")[:2000],
        checked_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(check)


def _capability_data(account: Account, result: dict[str, Any]) -> dict[str, Any]:
    # account_id 必须统一返回字符串：select 的 value 也是字符串，避免前端严格比较误判。
    return {
        "account_id": str(account.id),
        "platform_account_id": str(account.goofish_id or ""),
        "is_fish_shop": bool(result.get("is_fish_shop")),
        "support_sku_or_inventory": bool(result.get("support_sku_or_inventory")),
        "commission_config": result.get("commission_config") or {
            "title": "", "default_title": "", "tips": "", "percent": "", "max_commission": "", "tip_url": "",
        },
    }


def _material_data(item: FeatureRecord) -> dict[str, Any]:
    value = dict(item.payload or {})
    value.update({"id": item.id, "user_id": item.owner_id, "owner_id": item.owner_id, "created_at": item.created_at, "updated_at": item.updated_at})
    value.setdefault("title", value.get("name") or "未命名素材")
    value.setdefault("description", "")
    value.setdefault("price", 0)
    value.setdefault("images", [value.get("url")] if value.get("url") else [])
    value.setdefault("videos", [])
    value.setdefault("specifications", [])
    value.setdefault("sku_rows", [])
    value.setdefault("quantity", 1)
    value.setdefault("delivery_method", "express")
    value.setdefault("shipping_method", "free")
    value.setdefault("support_pickup", False)
    value.setdefault("postage", 0)
    value.setdefault("condition", "全新")
    value.setdefault("platform_category_path", [])
    value.setdefault("platform_attributes", [])
    value.setdefault("category_source", "manual")
    return value


@router.get("/materials")
async def list_publish_materials(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=2000), title: str | None = Query(None), category: str | None = Query(None), condition: str | None = Query(None), platform_category_id: str | None = Query(None),
    user=Depends(get_current_user), db: AsyncSession = Depends(get_session),
):
    statement = select(FeatureRecord).where(FeatureRecord.feature == "product-materials")
    if not _is_admin(user):
        statement = statement.where(FeatureRecord.owner_id == _uid(user))
    rows = [_material_data(row) for row in (await db.execute(statement.order_by(FeatureRecord.id.desc()))).scalars().all()]
    if title: rows = [row for row in rows if title.lower() in str(row.get("title") or "").lower()]
    if category: rows = [row for row in rows if str(row.get("category") or "") == category]
    if condition: rows = [row for row in rows if str(row.get("condition") or "") == condition]
    if platform_category_id: rows = [row for row in rows if str(row.get("platform_category_id") or "") == platform_category_id]
    total = len(rows); start = (page - 1) * page_size
    return ok({"list": rows[start:start + page_size], "items": rows[start:start + page_size], "total": total, "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size if total else 0}, "查询成功")


@router.post("/materials")
async def create_publish_material(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    if not str(payload.get("title") or payload.get("name") or "").strip():
        raise HTTPException(422, "素材标题不能为空")
    data = {**payload, "title": str(payload.get("title") or payload.get("name") or "").strip()}
    row = FeatureRecord(owner_id=_uid(user), feature="product-materials", external_id=uuid4().hex, status="active", payload=data, note="商品发布素材")
    db.add(row); await db.commit(); await db.refresh(row)
    return ok(_material_data(row), "素材已创建")


@router.get("/materials/{material_id}")
async def get_publish_material(material_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    statement = select(FeatureRecord).where(FeatureRecord.id == material_id, FeatureRecord.feature == "product-materials")
    if not _is_admin(user): statement = statement.where(FeatureRecord.owner_id == _uid(user))
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None: raise HTTPException(404, "素材不存在")
    return ok(_material_data(row), "查询成功")


@router.put("/materials/{material_id}")
async def update_publish_material(material_id: int, payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    statement = select(FeatureRecord).where(FeatureRecord.id == material_id, FeatureRecord.feature == "product-materials")
    if not _is_admin(user): statement = statement.where(FeatureRecord.owner_id == _uid(user))
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None: raise HTTPException(404, "素材不存在")
    row.payload = {**(row.payload or {}), **payload}; await db.commit(); await db.refresh(row)
    return ok(_material_data(row), "素材已更新")


@router.delete("/materials/{material_id}")
async def delete_publish_material(material_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    statement = select(FeatureRecord).where(FeatureRecord.id == material_id, FeatureRecord.feature == "product-materials")
    if not _is_admin(user): statement = statement.where(FeatureRecord.owner_id == _uid(user))
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None: raise HTTPException(404, "素材不存在")
    await db.delete(row); await db.commit()
    return ok({"id": material_id, "deleted": True}, "素材已删除")


@router.post("/materials/batch-delete")
async def batch_delete_publish_materials(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    ids = [int(value) for value in payload.get("ids") or [] if str(value).isdigit()]
    statement = select(FeatureRecord).where(FeatureRecord.feature == "product-materials", FeatureRecord.id.in_(ids))
    if not _is_admin(user): statement = statement.where(FeatureRecord.owner_id == _uid(user))
    rows = list((await db.execute(statement)).scalars().all()) if ids else []
    for row in rows: await db.delete(row)
    await db.commit()
    return ok({"deleted": len(rows), "ids": ids}, "素材已批量删除")


async def _detect_for_account(account: Account, db: AsyncSession) -> dict[str, Any]:
    if not (account.cookie or "").strip():
        result = {"success": False, "account_invalid": True, "message": "账号没有有效 Cookie，请先扫码登录", "cookies_str": ""}
    else:
        try:
            result = await detect_publish_capability(
                cookie=account.cookie,
                platform_account_id=str(account.goofish_id or account.id),
                proxy=account.proxy,
            )
        except Exception as exc:  # 外部平台异常必须转成页面可读错误
            result = {"success": False, "account_invalid": False, "message": f"账号发布能力检测失败：{exc}", "cookies_str": account.cookie}
    if result.get("account_invalid") and any(marker in str(result.get("message") or "").upper() for marker in ("SESSION", "COOKIE", "登录态")):
        # 让账号列表与真实平台状态一致；扫码登录成功后 qr_login 会恢复 active。
        account.status = "expired"
    await _save_latest_cookie(account, result.get("cookies_str") or account.cookie or "", db)
    await _record_capability(account, result, db)
    await db.commit()
    return result


@router.get("/accounts/{account_id}/capability")
async def get_publish_capability(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    account = await _owned_account(account_id, user, db)
    result = await _detect_for_account(account, db)
    if not result.get("success"):
        code = "account_invalid" if result.get("account_invalid") else "capability_unavailable"
        return error(result.get("message") or "账号发布能力检测失败", code=code, data={
            "account_id": str(account.id),
            "account_invalid": bool(result.get("account_invalid")),
        })
    return ok(_capability_data(account, result), "账号发布能力检测成功")


@router.post("/accounts/{account_id}/capability")
async def refresh_publish_capability(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    """兼容旧版主动检测按钮，但使用同一条真实检测链路。"""
    return await get_publish_capability(account_id, user, db)


def _dict_list(value: Any) -> list[dict[str, Any]]:
    """只保留分类切换协议需要的对象列表，避免把异常输入直接传给平台。"""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


@router.post("/category/recommend")
async def recommend_category(
    payload: dict[str, Any] = Body(default_factory=dict),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """按标题/描述调用闲鱼真实分类推荐接口。

    分类卡必须保存完整回传给前端，用户切换分类时平台才会返回对应的
    属性卡和发布所需的 channelCatId、catId（老分类可能另带 tbCatId）。该路由不能落到旧版兼容
    兜底层，否则只会记录 FeatureRecord 而不会得到任何分类。
    """
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    if not title and not description:
        return error("请先填写商品标题或商品描述", code="category_input_required")

    requested_account_id = payload.get("account_id")
    accounts: list[Account] = []
    if requested_account_id not in (None, ""):
        try:
            account = await _owned_account(int(requested_account_id), user, db)
        except (TypeError, ValueError):
            return error("指定的闲鱼账号无效", code="account_invalid")
        if not (account.cookie or "").strip():
            return error("指定的闲鱼账号缺少 Cookie，请重新登录账号", code="account_invalid")
        accounts = [account]
    else:
        statement = select(Account).where(Account.cookie.is_not(None)).order_by(Account.id.desc())
        if not _is_admin(user):
            statement = statement.where(Account.user_id == _uid(user))
        accounts = [item for item in (await db.execute(statement)).scalars().all() if (item.cookie or "").strip()]
        accounts.sort(key=lambda item: (str(item.status or "").lower() != "active", -int(item.id)))

    if not accounts:
        return error("当前没有可用于分类推荐的已登录账号，请先扫码登录账号", code="account_invalid")

    service = PlatformCategoryService()
    last_error = "分类推荐失败，请稍后重试"
    for index, account in enumerate(accounts):
        try:
            result = await service.recommend(
                title=title or description[:200],
                description=description or title,
                cookie=account.cookie or "",
                account_id=str(account.goofish_id or account.id),
                current_card_list=_dict_list(payload.get("current_card_list")),
                selected_list=_dict_list(payload.get("selected_list")),
                cat_id=str(payload.get("cat_id") or ""),
                cat_name=str(payload.get("cat_name") or ""),
                channel_cat_id=str(payload.get("channel_cat_id") or ""),
                proxy=account.proxy,
            )
            # 分类接口可能同时下发新的 _m_h5_tk；写回账号后下一次请求才
            # 能继续签名，Cookie 本身绝不能通过 API 返回给浏览器。
            await _save_latest_cookie(account, str(result.get("cookies_str") or ""), db)
            await db.commit()
            result.pop("cookies_str", None)
            result["account_id"] = str(account.id)
            return ok(result, "分类推荐成功")
        except CategoryRecommendationError as exc:
            last_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            last_error = "分类推荐失败，请稍后重试"
            logger.exception("商品分类推荐异常 account_id=%s: %s", account.id, exc)
        if index < len(accounts) - 1:
            logger.warning("分类推荐切换备用账号 user_id=%s failed_account_id=%s", _uid(user), account.id)

    return error(last_error, code="category_recommend_failed")


def _new_publish_record(account: Account, payload: dict[str, Any], status: str, note: str) -> FeatureRecord:
    return FeatureRecord(
        owner_id=int(account.user_id),
        feature="product-publish-jobs",
        external_id=uuid4().hex,
        status=status,
        payload={
            "user_id": int(account.user_id),
            "account_id": str(account.id),
            "title": str(payload.get("title") or ""),
            "description": payload.get("description"),
            "price": payload.get("price"),
            "item_id": None,
            "item_url": None,
            "error_message": note if status == "failed" else None,
        },
        note=note,
    )


BATCH_FEATURE = "product-publish-batches"


def _material_snapshot(item: FeatureRecord) -> dict[str, Any]:
    """将素材记录快照化，后台任务不依赖请求会话或已删除的素材。"""
    value = dict(item.payload or {})
    value.update({"id": item.id, "material_id": item.id})
    value.setdefault("title", value.get("name") or "未命名商品")
    value.setdefault("description", "")
    value.setdefault("price", 0)
    value.setdefault("images", [value.get("url")] if value.get("url") else [])
    return value


async def _update_batch_parent(session: AsyncSession, batch_id: str, *, status: str | None = None, extra: dict[str, Any] | None = None) -> None:
    parent = (
        await session.execute(
            select(FeatureRecord).where(FeatureRecord.feature == BATCH_FEATURE, FeatureRecord.external_id == batch_id)
        )
    ).scalar_one_or_none()
    if parent is None:
        return
    payload = {**(parent.payload or {}), **(extra or {})}
    if status:
        parent.status = status
    parent.payload = payload
    parent.note = str(payload.get("message") or parent.note or "")[:2000] or None
    await session.commit()


async def _run_batch_publish_background(
    batch_id: str,
    owner_id: int,
    account_ids: list[int],
    materials: list[dict[str, Any]],
) -> None:
    """逐账号、逐素材执行真实发布；每个子任务单独落库，支持刷新页面恢复进度。"""
    del owner_id  # 子任务已经按账号归属校验，后台只处理快照中的账号。
    async with async_session_maker() as session:
        children = list(
            (
                await session.execute(
                    select(FeatureRecord).where(
                        FeatureRecord.feature == "product-publish-jobs",
                        FeatureRecord.payload["batch_id"].as_string() == batch_id,
                    ).order_by(FeatureRecord.id.asc())
                )
            ).scalars().all()
        )
        parent = (
            await session.execute(
                select(FeatureRecord).where(FeatureRecord.feature == BATCH_FEATURE, FeatureRecord.external_id == batch_id)
            )
        ).scalar_one_or_none()
        if parent is None:
            return
        parent.status = "publishing"
        await session.commit()

        for account_id in account_ids:
            account = (await session.execute(select(Account).where(Account.id == account_id))).scalar_one_or_none()
            account_children = [row for row in children if str((row.payload or {}).get("account_id")) == str(account_id)]
            account_sync: dict[str, Any] = {"sync_status": "skipped", "sync_message": "该账号没有发布成功商品", "sync_total_count": 0, "sync_saved_count": 0}
            if account is None or not (account.cookie or "").strip():
                message = "账号不存在或没有有效 Cookie，请先扫码登录"
                for child in account_children:
                    child.status = "failed"
                    child.note = message
                    child.payload = {**(child.payload or {}), "error_message": message}
                account_sync = {"sync_status": "skipped", "sync_message": message, "sync_total_count": 0, "sync_saved_count": 0}
                await session.commit()
                continue

            try:
                capability = await _detect_for_account(account, session)
            except Exception as exc:  # capability failure must fail this account's jobs
                capability = {"success": False, "message": f"账号发布能力检测失败：{str(exc)[:400]}"}
            if not capability.get("success"):
                message = str(capability.get("message") or "账号发布能力检测失败")[:2000]
                for child in account_children:
                    child.status = "failed"
                    child.note = message
                    child.payload = {**(child.payload or {}), "error_message": message}
                await session.commit()
                continue

            for child in account_children:
                child.status = "publishing"
                child.note = "正在调用闲鱼发布接口"
                child.payload = {**(child.payload or {}), "error_message": None}
                await session.commit()
                material_id = int((child.payload or {}).get("material_id") or 0)
                material = next((item for item in materials if int(item.get("id") or 0) == material_id), None)
                if material is None:
                    result = {"success": False, "message": "批量任务中的素材快照不存在", "item_id": None, "item_url": None, "cookies_str": account.cookie}
                else:
                    try:
                        result = await publish_item(
                            item_data={**material, "account_id": str(account.id)},
                            cookie=account.cookie or "",
                            platform_account_id=str(account.goofish_id or account.id),
                            proxy=account.proxy,
                            is_fish_shop=bool(capability.get("is_fish_shop")),
                        )
                    except GoofishPublishError as exc:
                        result = {"success": False, "account_invalid": exc.account_invalid, "message": str(exc), "item_id": None, "item_url": None, "cookies_str": account.cookie}
                    except Exception as exc:  # noqa: BLE001
                        result = {"success": False, "account_invalid": False, "message": f"发布执行失败：{str(exc)[:500]}", "item_id": None, "item_url": None, "cookies_str": account.cookie}
                await _save_latest_cookie(account, result.get("cookies_str") or account.cookie or "", session)
                success = bool(result.get("success"))
                message = str(result.get("message") or ("商品发布成功" if success else "商品发布失败"))[:2000]
                child.status = "success" if success else "failed"
                child.note = message
                child.payload = {
                    **(child.payload or {}),
                    "item_id": result.get("item_id"),
                    "item_url": result.get("item_url"),
                    "error_message": None if success else message,
                }
                await session.commit()

            successful = [row for row in account_children if row.status == "success"]
            if successful:
                account_sync = {"sync_status": "running", "sync_message": "发布成功，正在同步该账号商品", "sync_total_count": 0, "sync_saved_count": 0}
                for row in account_children:
                    row.payload = {**(row.payload or {}), **account_sync}
                await session.commit()
                try:
                    sync_result = await sync_account_products(
                        session, account, page_size=30, max_pages=100, sync_products=True, sync_orders=False,
                    )
                    account_sync = {
                        "sync_status": "success" if sync_result.get("status") == "success" else "failed",
                        "sync_message": str(sync_result.get("message") or "商品同步完成")[:1000],
                        "sync_total_count": int(sync_result.get("fetched_count") or sync_result.get("total_count") or 0),
                        "sync_saved_count": int(sync_result.get("saved_count") or 0),
                    }
                except Exception as exc:
                    account_sync = {"sync_status": "unknown", "sync_message": f"发布成功但商品同步未确认：{str(exc)[:800]}", "sync_total_count": 0, "sync_saved_count": 0}
                for row in account_children:
                    row.payload = {**(row.payload or {}), **account_sync}
                await session.commit()

        children = list(
            (
                await session.execute(
                    select(FeatureRecord).where(
                        FeatureRecord.feature == "product-publish-jobs",
                        FeatureRecord.payload["batch_id"].as_string() == batch_id,
                    )
                )
            ).scalars().all()
        )
        success_count = sum(row.status == "success" for row in children)
        failed_count = sum(row.status == "failed" for row in children)
        pending_count = sum(row.status == "pending" for row in children)
        publishing_count = sum(row.status == "publishing" for row in children)
        final_status = "success" if success_count == len(children) and children else "partial" if success_count else "failed"
        await _update_batch_parent(session, batch_id, status=final_status, extra={"message": "批量发布完成", "success": success_count, "failed": failed_count})


@router.post("/publish/batch")
async def publish_batch(
    background_tasks: BackgroundTasks,
    payload: dict[str, Any] = Body(default_factory=dict),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    raw_accounts = payload.get("account_ids") or []
    raw_materials = payload.get("material_ids") or []
    try:
        account_ids = list(dict.fromkeys(int(value) for value in raw_accounts))
        material_ids = list(dict.fromkeys(int(value) for value in raw_materials))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="账号或素材ID格式无效") from exc
    if not account_ids or not material_ids:
        return error("请至少选择一个账号和一条素材", code="batch_selection_required")
    account_statement = select(Account).where(Account.id.in_(account_ids))
    material_statement = select(FeatureRecord).where(FeatureRecord.feature == "product-materials", FeatureRecord.id.in_(material_ids))
    if not _is_admin(user):
        account_statement = account_statement.where(Account.user_id == _uid(user))
        material_statement = material_statement.where(FeatureRecord.owner_id == _uid(user))
    accounts = list((await db.execute(account_statement)).scalars().all())
    materials = list((await db.execute(material_statement)).scalars().all())
    if len(accounts) != len(account_ids):
        return error("部分账号不存在或无权使用，请重新选择账号", code="account_invalid")
    if len(materials) != len(material_ids):
        return error("部分素材不存在或无权使用，请重新选择素材", code="material_invalid")
    batch_id = f"batch-{uuid4().hex}"
    snapshots = [_material_snapshot(item) for item in materials]
    owner_id = _uid(user) if not _is_admin(user) else int(accounts[0].user_id)
    parent = FeatureRecord(
        owner_id=owner_id, feature=BATCH_FEATURE, external_id=batch_id, status="pending",
        payload={"batch_id": batch_id, "account_ids": account_ids, "material_ids": material_ids, "total": len(account_ids) * len(materials)},
        note="批量发布任务已创建",
    )
    db.add(parent)
    for account in accounts:
        for material in snapshots:
            child_payload = {
                "batch_id": batch_id, "account_id": str(account.id), "material_id": material["id"],
                "title": material.get("title") or "未命名商品", "description": material.get("description"),
                "price": material.get("price"), "item_id": None, "item_url": None, "error_message": None,
            }
            db.add(FeatureRecord(owner_id=int(account.user_id), feature="product-publish-jobs", external_id=uuid4().hex, status="pending", payload=child_payload, note="等待发布"))
    await db.commit()
    background_tasks.add_task(_run_batch_publish_background, batch_id, owner_id, account_ids, snapshots)
    return ok({"batch_id": batch_id, "total": len(account_ids) * len(materials)}, "批量发布任务已提交")


@router.get("/publish/batch/{batch_id}/status")
async def publish_batch_status(batch_id: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    parent_statement = select(FeatureRecord).where(FeatureRecord.feature == BATCH_FEATURE, FeatureRecord.external_id == batch_id)
    if not _is_admin(user):
        parent_statement = parent_statement.where(FeatureRecord.owner_id == _uid(user))
    parent = (await db.execute(parent_statement)).scalar_one_or_none()
    if parent is None:
        raise HTTPException(status_code=404, detail="批量发布任务不存在")
    children = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == "product-publish-jobs", FeatureRecord.payload["batch_id"].as_string() == batch_id).order_by(FeatureRecord.id.asc()))).scalars().all())
    success_count = sum(row.status == "success" for row in children)
    failed_count = sum(row.status == "failed" for row in children)
    publishing_count = sum(row.status == "publishing" for row in children)
    pending_count = sum(row.status == "pending" for row in children)
    account_ids = [str(value) for value in ((parent.payload or {}).get("account_ids") or [])]
    statuses = []
    for account_id in account_ids:
        rows = [row for row in children if str((row.payload or {}).get("account_id")) == account_id]
        sync_values = [(row.payload or {}) for row in rows if (row.payload or {}).get("sync_status")]
        sync = sync_values[-1] if sync_values else {}
        statuses.append({
            "account_id": account_id, "total": len(rows), "success": sum(row.status == "success" for row in rows),
            "failed": sum(row.status == "failed" for row in rows), "publishing": sum(row.status == "publishing" for row in rows),
            "pending": sum(row.status == "pending" for row in rows), "sync_status": sync.get("sync_status", "pending"),
            "sync_message": str(sync.get("sync_message") or "等待该账号发布完成后自动获取商品"),
            "sync_total_count": int(sync.get("sync_total_count") or 0), "sync_saved_count": int(sync.get("sync_saved_count") or 0),
        })
    return ok({
        "batch_id": batch_id, "total": len(children), "success": success_count, "failed": failed_count,
        "publishing": publishing_count, "pending": pending_count, "finished": pending_count == 0 and publishing_count == 0,
        "account_statuses": statuses,
    }, "查询成功")


@router.post("/publish/single")
async def publish_single(payload: dict[str, Any] = Body(default_factory=dict), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    raw_account_id = payload.get("account_id")
    try:
        account_id = int(raw_account_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="请选择有效的发布账号") from exc
    account = await _owned_account(account_id, user, db)
    if not (account.cookie or "").strip():
        return error("账号没有有效 Cookie，请先扫码登录", code="account_invalid", data={"account_id": str(account.id)})

    # 发布前重新探测一次，避免前端缓存的能力与实际账号状态不一致。
    capability = await _detect_for_account(account, db)
    if not capability.get("success"):
        code = "account_invalid" if capability.get("account_invalid") else "capability_unavailable"
        return error(capability.get("message") or "账号发布能力检测失败", code=code, data={"account_id": str(account.id)})

    record = _new_publish_record(account, payload, "publishing", "正在调用闲鱼发布接口")
    db.add(record)
    await db.commit()
    await db.refresh(record)
    try:
        result = await publish_item(
            item_data=payload,
            cookie=account.cookie or "",
            platform_account_id=str(account.goofish_id or account.id),
            proxy=account.proxy,
            is_fish_shop=bool(capability.get("is_fish_shop")),
        )
    except GoofishPublishError as exc:
        result = {"success": False, "account_invalid": exc.account_invalid, "message": str(exc), "item_id": None, "item_url": None, "cookies_str": account.cookie}
    except Exception as exc:  # noqa: BLE001
        result = {"success": False, "account_invalid": False, "message": f"发布执行失败：{exc}", "item_id": None, "item_url": None, "cookies_str": account.cookie}

    await _save_latest_cookie(account, result.get("cookies_str") or account.cookie or "", db)
    record.status = "success" if result.get("success") else "failed"
    record.note = str(result.get("message") or ("商品发布成功" if result.get("success") else "商品发布失败"))[:2000]
    record.payload = {
        **(record.payload or {}),
        "item_id": result.get("item_id"),
        "item_url": result.get("item_url"),
        "error_message": None if result.get("success") else record.note,
    }
    await db.commit()
    data = {
        "item_url": result.get("item_url"),
        "item_id": result.get("item_id"),
        "log_id": record.id,
        "sync_status": "skipped",
        "sync_message": "发布成功后商品同步将在下一次账号同步时执行" if result.get("success") else None,
        "sync_total_count": 0,
        "sync_saved_count": 0,
    }
    if not result.get("success"):
        code = "account_invalid" if result.get("account_invalid") else "publish_failed"
        return error(result.get("message") or "商品发布失败", code=code, data=data)
    return ok(data, result.get("message") or "商品发布成功")


@router.get("/logs")
async def list_publish_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    account_id: str | None = Query(None),
    status: str | None = Query(None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    statement = select(FeatureRecord).where(FeatureRecord.feature == "product-publish-jobs")
    if not _is_admin(user):
        statement = statement.where(FeatureRecord.owner_id == _uid(user))
    rows = list((await db.execute(statement.order_by(FeatureRecord.id.desc()))).scalars().all())
    if account_id:
        rows = [row for row in rows if str((row.payload or {}).get("account_id")) == str(account_id)]
    if status:
        rows = [row for row in rows if row.status == status]
    total = len(rows)
    start = (page - 1) * page_size
    items = []
    for row in rows[start:start + page_size]:
        payload = dict(row.payload or {})
        payload.update({"id": row.id, "owner_id": row.owner_id, "status": row.status, "note": row.note, "created_at": row.created_at, "updated_at": row.updated_at})
        items.append(payload)
    return ok({"list": items, "total": total, "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size if total else 0}, "查询成功")


@router.delete("/logs/clear")
async def clear_publish_logs(user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    statement = delete(FeatureRecord).where(FeatureRecord.feature == "product-publish-jobs")
    if not _is_admin(user):
        statement = statement.where(FeatureRecord.owner_id == _uid(user))
    result = await db.execute(statement)
    await db.commit()
    return ok({"deleted": int(result.rowcount or 0)}, "发布日志已清理")


@router.get("/addresses/input-tips")
async def input_tips(keywords: str = Query(..., min_length=1, max_length=100), city: str = Query("全国", max_length=32), user=Depends(get_current_user)):
    del user
    try:
        return ok(await search_input_tips(keywords, city), "所在地搜索成功")
    except AmapInputTipsError as exc:
        return error(str(exc), code="address_search_failed")
