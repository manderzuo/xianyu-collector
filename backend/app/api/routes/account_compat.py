# -*- coding: utf-8 -*-
"""账号列表旧接口到真实账号表的兼容实现。

旧前端把账号主键称为 cookie_id，并把账号设置拆成很多小接口。重写版
统一以账号主键操作，并将账号级配置持久化到账号设置记录中。
"""
from __future__ import annotations

from datetime import datetime, timezone
import csv
import io
import re
import zipfile
import httpx
from html import escape as xml_escape
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.services.account_settings import load_account_settings, load_account_settings_map, save_account_settings
from common.db.session import get_session
from common.config import settings as app_settings
from common.models.account_cookies import AccountCookie
from common.models.accounts import Account
from common.models.operations import Upload
from common.services.account_renewal import renew_account_session
from common.services.xianyu_platform import XianyuPlatformError, close_account_notice
from common.services.ai_provider_service import test_ai_connection
from backend.app.api.routes.accounts import delete_account


router = APIRouter(prefix="/api/v1/cookies", tags=["账号列表兼容操作"])


def _safe_xml_text(value: Any) -> str:
    """移除 XML 1.0 不允许的控制字符，避免导出文件损坏。"""
    text = "" if value is None else str(value)
    text = "".join(char for char in text if char in "\t\n\r" or ord(char) >= 0x20)
    return xml_escape(text, quote=True)


def _column_name(index: int) -> str:
    name = ""
    while index >= 0:
        index, remainder = divmod(index, 26)
        name = chr(65 + remainder) + name
        index -= 1
    return name


def _build_xlsx(headers: list[str], rows: list[list[Any]]) -> bytes:
    """用标准库生成一个不依赖额外 Excel 包的有效 XLSX 文件。"""
    xml_rows: list[str] = []
    for row_number, row in enumerate([headers, *rows], start=1):
        cells = []
        for index, value in enumerate(row):
            cell_ref = f"{_column_name(index)}{row_number}"
            cells.append(
                f'<c r="{cell_ref}" t="inlineStr"><is><t>{_safe_xml_text(value)}</t></is></c>'
            )
        xml_rows.append(f'<row r="{row_number}">' + "".join(cells) + "</row>")

    last_ref = f"{_column_name(max(len(headers) - 1, 0))}{max(len(rows) + 1, 1)}"
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_ref}"/><sheetData>{"".join(xml_rows)}</sheetData>'
        '</worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="账号" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    package_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xlsx_rows(raw: bytes) -> list[list[str]]:
    """读取常见 Excel XLSX（含 inline string/shared string）单元格。"""
    from xml.etree import ElementTree as ElementTree

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in shared_root:
                if _xml_local_name(item.tag) == "si":
                    shared_strings.append("".join(item.itertext()))

        worksheet_name = "xl/worksheets/sheet1.xml"
        if worksheet_name not in archive.namelist():
            worksheet_files = [name for name in archive.namelist() if name.startswith("xl/worksheets/") and name.endswith(".xml")]
            if not worksheet_files:
                raise ValueError("Excel文件缺少工作表")
            worksheet_name = worksheet_files[0]
        root = ElementTree.fromstring(archive.read(worksheet_name))
        result: list[list[str]] = []
        for row_element in root.iter():
            if _xml_local_name(row_element.tag) != "row":
                continue
            cells: dict[int, str] = {}
            next_index = 0
            for cell in row_element:
                if _xml_local_name(cell.tag) != "c":
                    continue
                reference = cell.attrib.get("r", "")
                match = re.match(r"([A-Za-z]+)", reference)
                if match:
                    column_index = 0
                    for character in match.group(1).upper():
                        column_index = column_index * 26 + ord(character) - 64
                    column_index -= 1
                else:
                    column_index = next_index
                next_index = column_index + 1
                cell_type = cell.attrib.get("t")
                value_element = next((child for child in cell if _xml_local_name(child.tag) == "v"), None)
                inline_element = next((child for child in cell if _xml_local_name(child.tag) == "is"), None)
                if cell_type == "inlineStr" and inline_element is not None:
                    value = "".join(inline_element.itertext())
                else:
                    value = "" if value_element is None else "".join(value_element.itertext())
                    if cell_type == "s" and value.isdigit():
                        value = shared_strings[int(value)] if int(value) < len(shared_strings) else ""
                cells[column_index] = value
            if cells:
                result.append([cells.get(index, "") for index in range(max(cells) + 1)])
        return result


def _import_rows(raw: bytes) -> list[dict[str, str]]:
    if zipfile.is_zipfile(io.BytesIO(raw)):
        rows = _xlsx_rows(raw)
    else:
        try:
            rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        except UnicodeDecodeError as exc:
            raise ValueError("仅支持有效的 XLSX 文件") from exc
    if not rows:
        return []
    headers = [str(value).strip() for value in rows[0]]
    return [
        {headers[index]: str(value).strip() for index, value in enumerate(row) if index < len(headers) and headers[index]}
        for row in rows[1:]
        if any(str(value).strip() for value in row)
    ]


def _row_value(row: dict[str, str], *names: str) -> str:
    normalized = {key.strip().lower().replace(" ", ""): value for key, value in row.items()}
    for name in names:
        value = normalized.get(name.strip().lower().replace(" ", ""))
        if value is not None and value != "":
            return value
    return ""


def _row_has(row: dict[str, str], *names: str) -> bool:
    normalized = {key.strip().lower().replace(" ", "") for key in row}
    return any(name.strip().lower().replace(" ", "") in normalized for name in names)


def _as_bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "是", "开启", "启用", "active", "on"}


def _as_int(value: str, default: int = 0) -> int:
    try:
        return int(float(value)) if value else default
    except (TypeError, ValueError):
        return default


def _uid(user: dict[str, Any]) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


async def _account(account_id: int, user: dict[str, Any], db: AsyncSession) -> Account:
    statement = select(Account).where(Account.id == account_id)
    if not _is_admin(user):
        statement = statement.where(Account.user_id == _uid(user))
    account = (await db.execute(statement)).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return account


async def _save_action(
    account_id: int,
    user: dict[str, Any],
    db: AsyncSession,
    values: dict[str, Any],
) -> dict[str, Any]:
    account = await _account(account_id, user, db)
    return await save_account_settings(db, int(account.user_id), account_id, values)


@router.put("/status/batch")
async def batch_status(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    data = payload or {}
    account_ids = [int(item) for item in data.get("account_ids", []) if str(item).isdigit()]
    enabled = bool(data.get("enabled", True))
    success_ids: list[str] = []
    failed_items: list[dict[str, str]] = []
    for account_id in account_ids:
        try:
            account = await _account(account_id, user, db)
            account.status = "active" if enabled else "inactive"
            success_ids.append(str(account_id))
        except HTTPException as exc:
            failed_items.append({"account_id": str(account_id), "message": str(exc.detail)})
    await db.commit()
    runtime_results: list[dict[str, Any]] = []
    for account_id in list(success_ids):
        account = await _account(int(account_id), user, db)
        action = "start" if enabled and (account.cookie or "").strip() else "stop"
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.post(
                    f"{app_settings.websocket_service_url.rstrip('/')}/internal/accounts/{account.id}/{action}",
                    json={"cookie_value": account.cookie or "", "user_id": int(account.user_id)},
                    headers={"X-Internal-Token": app_settings.jwt_secret},
                )
            value = response.json()
            runtime_ok = bool(response.is_success and value.get("success"))
            runtime_results.append({"account_id": account.id, "success": runtime_ok, "message": value.get("message")})
            if not runtime_ok and action == "start":
                success_ids.remove(str(account_id))
                failed_items.append({"account_id": str(account_id), "message": str(value.get("message") or "账号连接启动失败")[:500]})
        except (httpx.HTTPError, ValueError) as exc:
            runtime_results.append({"account_id": account.id, "success": False, "message": str(exc)[:300]})
            if action == "start":
                success_ids.remove(str(account_id))
                failed_items.append({"account_id": str(account_id), "message": f"账号连接服务不可用：{str(exc)[:300]}"})
    return ok(
        {
            "success_count": len(success_ids),
            "failed_count": len(failed_items),
            "success_ids": success_ids,
            "failed_items": failed_items,
            "runtime_results": runtime_results,
        },
        "批量启用成功" if enabled else "批量禁用成功",
    )


@router.put("/close-notice/batch")
async def batch_close_notice(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    account_ids = [int(item) for item in (payload or {}).get("account_ids", []) if str(item).isdigit()]
    success_ids: list[str] = []
    failed_items: list[dict[str, str]] = []
    for account_id in account_ids:
        try:
            account = await _account(account_id, user, db)
            if not (account.cookie or "").strip():
                raise RuntimeError("账号没有有效 Cookie")
            result = await close_account_notice(
                cookies=account.cookie,
                account_id=str(account.goofish_id or account.id),
            )
            latest_cookie = str(result.get("cookies_str") or account.cookie)
            if latest_cookie != account.cookie:
                account.cookie = latest_cookie
                db.add(AccountCookie(account_id=account.id, cookie_value=latest_cookie, status="active"))
            await _save_action(
                account_id,
                user,
                db,
                {"close_notice": True, "close_notice_at": datetime.now(timezone.utc).isoformat()},
            )
            success_ids.append(str(account_id))
        except (HTTPException, XianyuPlatformError, RuntimeError) as exc:
            failed_items.append({"account_id": str(account_id), "message": str(getattr(exc, "detail", exc))[:500]})
        except Exception as exc:
            await db.rollback()
            failed_items.append({"account_id": str(account_id), "message": str(exc)[:500]})
    return ok(
        {"success_count": len(success_ids), "failed_count": len(failed_items), "success_ids": success_ids, "failed_items": failed_items},
        "批量关闭通知完成",
    )


@router.put("/clear-token-cache/batch")
async def batch_clear_token_cache(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    account_ids = [int(item) for item in (payload or {}).get("account_ids", []) if str(item).isdigit()]
    timestamp = datetime.now(timezone.utc).isoformat()
    success_ids: list[str] = []
    failed_items: list[dict[str, str]] = []
    for account_id in account_ids:
        try:
            account = await _account(account_id, user, db)
            if not (account.cookie or "").strip():
                raise RuntimeError("账号没有有效 Cookie")
            await _save_action(account_id, user, db, {"token_cache_cleared_at": timestamp})
            # Token 存在于连接服务的账号运行时内存中；重启运行时会
            # 重新按当前 Token获取方式申请 Token，不能只写一个时间戳。
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{app_settings.websocket_service_url.rstrip('/')}/internal/accounts/{account.id}/restart",
                    json={"cookie_value": account.cookie, "user_id": int(account.user_id)},
                    headers={"X-Internal-Token": app_settings.jwt_secret},
                )
            value = response.json()
            if not response.is_success or not value.get("success"):
                raise RuntimeError(str(value.get("message") or "连接服务拒绝重启"))
            success_ids.append(str(account_id))
        except (HTTPException, RuntimeError, httpx.HTTPError, ValueError) as exc:
            await db.rollback()
            failed_items.append({"account_id": str(account_id), "message": str(getattr(exc, "detail", exc))[:500]})
    return ok(
        {"success_count": len(success_ids), "failed_count": len(failed_items), "success_ids": success_ids, "failed_items": failed_items},
        "清除Token缓存并重启完成",
    )


@router.post("/renew-login")
async def batch_renew_login(
    payload: Any = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    if isinstance(payload, list):
        raw_ids = payload
    elif isinstance(payload, dict):
        raw_ids = payload.get("account_ids", [])
    elif payload is None:
        raw_ids = []
    else:
        # 兼容旧页面单选时直接提交一个数字，而不是 [数字]。
        raw_ids = [payload]
    account_ids = [int(item) for item in raw_ids if str(item).isdigit()]
    results = []
    for account_id in account_ids:
        try:
            account = await _account(account_id, user, db)
            result = await renew_account_session(
                db,
                account,
                source="manual",
                force=True,
                notify_runtime=True,
            )
            results.append({
                "account_id": account.id,
                "account_name": account.account_name,
                "success": bool(result.get("success")),
                "status": result.get("status"),
                "method": result.get("method"),
                "message": result.get("message"),
                "updated_cookie_names": result.get("updated_cookie_names", []),
                "runtime": result.get("runtime"),
            })
        except HTTPException as exc:
            results.append({"account_id": account_id, "account_name": str(account_id), "success": False, "status": "failed", "message": str(exc.detail)})
        except Exception as exc:
            await db.rollback()
            results.append({"account_id": account_id, "account_name": str(account_id), "success": False, "status": "failed", "message": f"续期执行异常：{str(exc)[:500]}"})
    return ok(
        {
            "success_count": sum(1 for item in results if item["success"]),
            "failed_count": sum(1 for item in results if not item["success"]),
            "results": results,
        },
        "批量账号续期执行完成",
    )


ACCOUNT_EXPORT_HEADERS = [
    "内部账号ID", "账号ID", "闲鱼账号ID", "备注", "Cookie", "状态", "用户名", "密码", "显示浏览器",
    "AI回复", "定时补发货", "定时补评价", "商品擦亮", "自动确认收货", "发货成功再发卡券",
    "卡券发送成功再确认发货", "只发卡券不确认发货", "自动求小红花", "已下单用户禁止AI回复",
    "暂停时长", "相同消息等待时间", "自动回复延迟（秒）",
]


@router.post("/export")
async def export_accounts(
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """导出真实账号表和账号级配置，供备份或迁移使用。"""
    values = payload or {}
    result = await db.execute(
        select(Account).where(Account.user_id == _uid(user)).order_by(Account.id.desc())
    )
    accounts = list(result.scalars().all())
    requested_ids = {
        int(item) for item in (values.get("account_ids") or [])
        if str(item).isdigit()
    }
    if requested_ids:
        accounts = [item for item in accounts if item.id in requested_ids]
    status = str(values.get("status") or "").strip().lower()
    if status in {"active", "inactive", "paused", "expired"}:
        accounts = [item for item in accounts if item.status == status]
    account_keyword = str(values.get("account_id") or "").strip()
    if account_keyword:
        accounts = [
            item for item in accounts
            if account_keyword in str(item.id)
            or account_keyword in (item.goofish_id or "")
            or account_keyword in item.account_name
        ]
    settings_map = await load_account_settings_map(db, _uid(user), [item.id for item in accounts])
    has_password = values.get("has_password")
    if has_password is not None:
        expected = bool(has_password)
        accounts = [
            item for item in accounts
            if bool((settings_map.get(item.id) or {}).get("login_password")) == expected
        ]

    rows = []
    status_labels = {"active": "启用", "inactive": "禁用", "paused": "暂停", "expired": "过期"}
    for account in accounts:
        account_settings = settings_map.get(account.id) or {}
        rows.append([
            account.id,
            account.goofish_id or account.account_name,
            account.goofish_id or "",
            account.account_name,
            account.cookie or "",
            status_labels.get(account.status, account.status),
            account_settings.get("username", ""),
            account_settings.get("login_password", ""),
            "是" if account_settings.get("show_browser") else "否",
            "是" if account_settings.get("ai_enabled") else "否",
            "是" if account_settings.get("scheduled_redelivery") else "否",
            "是" if account_settings.get("scheduled_rate") else "否",
            "是" if account_settings.get("auto_polish") else "否",
            "是" if account_settings.get("auto_confirm") else "否",
            "是" if account_settings.get("confirm_before_send") else "否",
            "是" if account_settings.get("send_before_confirm") else "否",
            "是" if account_settings.get("only_send_card") else "否",
            "是" if account_settings.get("auto_red_flower") else "否",
            "是" if account_settings.get("ai_reply_block_ordered_users") else "否",
            account_settings.get("pause_duration", 0),
            account_settings.get("message_expire_time", 0),
            account_settings.get("reply_delay_seconds", 0),
        ])
    workbook = _build_xlsx(ACCOUNT_EXPORT_HEADERS, rows)
    return StreamingResponse(
        io.BytesIO(workbook),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="accounts.xlsx"'},
    )


@router.post("/import")
async def import_accounts(
    file: UploadFile = File(...),
    enable_all: bool = Form(False),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """导入账号备份；同一账号优先更新，不重复创建。"""
    raw = await file.read()
    try:
        rows = _import_rows(raw)
    except (ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=400, detail=f"Excel文件无法读取：{exc}") from exc
    if not rows:
        raise HTTPException(status_code=400, detail="Excel文件没有可导入的数据")

    owner_id = _uid(user)
    existing = list((await db.execute(select(Account).where(Account.user_id == owner_id))).scalars().all())
    by_internal_id = {int(item.id): item for item in existing}
    by_platform_id = {item.goofish_id: item for item in existing if item.goofish_id}
    by_name = {item.account_name: item for item in existing}
    inserted = 0
    updated = 0
    started = 0
    errors: list[str] = []
    status_map = {
        "active": "active", "启用": "active", "已启用": "active", "正常": "active",
        "inactive": "inactive", "禁用": "inactive", "已禁用": "inactive",
        "paused": "paused", "暂停": "paused", "expired": "expired", "过期": "expired",
    }
    boolean_fields = {
        "ai_enabled": ("AI回复", "ai_enabled"),
        "scheduled_redelivery": ("定时补发货", "scheduled_redelivery"),
        "scheduled_rate": ("定时补评价", "scheduled_rate"),
        "auto_polish": ("商品擦亮", "auto_polish"),
        "auto_confirm": ("自动确认收货", "auto_confirm"),
        "confirm_before_send": ("发货成功再发卡券", "confirm_before_send"),
        "send_before_confirm": ("卡券发送成功再确认发货", "send_before_confirm"),
        "only_send_card": ("只发卡券不确认发货", "only_send_card"),
        "auto_red_flower": ("自动求小红花", "auto_red_flower"),
        "ai_reply_block_ordered_users": ("已下单用户禁止AI回复", "ai_reply_block_ordered_users"),
        "show_browser": ("显示浏览器", "show_browser"),
    }
    for row_number, row in enumerate(rows, start=2):
        try:
            internal_id = _row_value(row, "内部账号ID", "internal_id", "internalid")
            platform_id = _row_value(row, "闲鱼账号ID", "goofish_id", "平台账号ID")
            account_name = _row_value(row, "备注", "账号名称", "account_name", "账号ID")
            if not platform_id:
                platform_id = account_name
            if not account_name:
                account_name = platform_id
            if not account_name:
                raise ValueError("缺少账号ID或账号名称")
            account_name = account_name[:64]
            platform_id = platform_id[:64] if platform_id else None
            account = None
            if internal_id.isdigit():
                account = by_internal_id.get(int(internal_id))
            if account is None and platform_id:
                account = by_platform_id.get(platform_id)
            if account is None:
                account = by_name.get(account_name)

            if account is None:
                account = Account(
                    user_id=owner_id,
                    account_name=account_name,
                    goofish_id=platform_id,
                    status="active" if enable_all else status_map.get(
                        _row_value(row, "状态", "status").lower(), "inactive"
                    ),
                )
                db.add(account)
                inserted += 1
            else:
                account.account_name = account_name
                if platform_id:
                    account.goofish_id = platform_id
                if not enable_all:
                    raw_status = _row_value(row, "状态", "status").lower()
                    if raw_status in status_map:
                        account.status = status_map[raw_status]
                else:
                    account.status = "active"
                updated += 1
            if enable_all:
                account.status = "active"
            cookie = _row_value(row, "Cookie", "cookie", "cookie_value")
            if cookie:
                account.cookie = cookie
            await db.flush()
            if cookie:
                db.add(AccountCookie(account_id=account.id, cookie_value=cookie, status="active"))

            settings_values: dict[str, Any] = {}
            for field, names in boolean_fields.items():
                if _row_has(row, *names):
                    settings_values[field] = _as_bool(_row_value(row, *names))
            for field, names in {
                "username": ("用户名", "username"),
                "login_password": ("密码", "login_password", "password"),
            }.items():
                if _row_has(row, *names):
                    settings_values[field] = _row_value(row, *names)
            for field, names in {
                "pause_duration": ("暂停时长", "pause_duration"),
                "message_expire_time": ("相同消息等待时间", "message_expire_time"),
                "reply_delay_seconds": ("自动回复延迟（秒）", "reply_delay_seconds"),
            }.items():
                if _row_has(row, *names):
                    settings_values[field] = _as_int(_row_value(row, *names))
            if settings_values:
                await save_account_settings(db, owner_id, account.id, settings_values)
            else:
                await db.commit()
            await db.refresh(account)
            by_internal_id[int(account.id)] = account
            if account.goofish_id:
                by_platform_id[account.goofish_id] = account
            by_name[account.account_name] = account
            if account.status == "active":
                started += 1
        except Exception as exc:
            await db.rollback()
            errors.append(f"第{row_number}行：{exc}")

    return ok(
        {
            "inserted": inserted,
            "updated": updated,
            "started": started,
            "failed": len(errors),
            "errors": errors,
        },
        f"账号导入完成：新增 {inserted} 个，更新 {updated} 个",
    )


@router.get("/{account_id}/settings")
async def get_settings(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    await _account(account_id, user, db)
    return ok(await load_account_settings(db, _uid(user), account_id), "账号设置查询成功")


@router.put("/{account_id}/settings")
async def put_settings(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    values = payload or {}
    account = await _account(account_id, user, db)
    proxy_config = values.get("proxy_config")
    if isinstance(proxy_config, dict):
        proxy_type = str(proxy_config.get("proxy_type") or "none")
        if proxy_type == "none":
            account.proxy = None
        else:
            host = str(proxy_config.get("proxy_host") or "").strip()
            port = str(proxy_config.get("proxy_port") or "").strip()
            account.proxy = f"{proxy_type}://{host}:{port}" if host and port else None
    settings = await _save_action(account_id, user, db, values)
    return ok(settings, "账号设置已更新")


@router.put("/{account_id}/remark")
async def update_remark(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    account = await _account(account_id, user, db)
    remark = str((payload or {}).get("remark") or "").strip()
    if remark:
        account.account_name = remark
        await db.commit()
        await db.refresh(account)
    return ok({"id": account_id, "remark": account.account_name}, "账号备注已更新")


@router.get("/{account_id}/delivery-block-rules")
async def get_delivery_block_rules(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    settings = await _save_action(account_id, user, db, {})
    stored_rules = settings.get("delivery_block_rules") or []
    if stored_rules:
        return ok(stored_rules, "禁止发货规则查询成功")
    available = [
        {"rule_code": "unpaid", "rule_name": "未付款订单", "rule_description": "未付款订单不允许发货", "enabled": False, "priority": 1, "block_reason": "", "auto_close_order": False, "only_card_after_close": False, "excluded_item_ids": [], "config": {}, "default_config": {}},
        {"rule_code": "refunding", "rule_name": "退款中订单", "rule_description": "退款中的订单不允许发货", "enabled": False, "priority": 2, "block_reason": "", "auto_close_order": False, "only_card_after_close": False, "excluded_item_ids": [], "config": {}, "default_config": {}},
        {"rule_code": "cancelled", "rule_name": "已取消订单", "rule_description": "已取消订单不允许发货", "enabled": False, "priority": 3, "block_reason": "", "auto_close_order": False, "only_card_after_close": False, "excluded_item_ids": [], "config": {}, "default_config": {}},
    ]
    return ok(available, "禁止发货规则查询成功")


@router.put("/{account_id}/delivery-block-rules")
async def put_delivery_block_rules(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    rules = (payload or {}).get("rules", [])
    settings = await _save_action(account_id, user, db, {"delivery_block_rules": rules})
    return ok(settings.get("delivery_block_rules", []), "禁止发货规则已更新")


@router.get("/delivery-block-rules/available")
async def available_delivery_block_rules(user=Depends(get_current_user)):
    return ok(
        [
            {"rule_code": "unpaid", "rule_name": "未付款订单", "rule_description": "未付款订单不允许发货"},
            {"rule_code": "refunding", "rule_name": "退款中订单", "rule_description": "退款中的订单不允许发货"},
            {"rule_code": "cancelled", "rule_name": "已取消订单", "rule_description": "已取消订单不允许发货"},
        ],
        "规则类型查询成功",
    )


@router.get("/{account_id}/default-reply")
async def get_default_reply(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    settings = await _save_action(account_id, user, db, {})
    return ok(settings.get("default_reply", {}), "默认回复查询成功")


@router.put("/{account_id}/default-reply")
async def put_default_reply(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    values = payload or {}
    settings = await _save_action(account_id, user, db, {"default_reply": values})
    return ok(settings.get("default_reply", {}), "默认回复已保存")


@router.get("/{account_id}/auto-rate")
async def get_auto_rate(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    settings = await _save_action(account_id, user, db, {})
    return ok({"account_id": str(account_id), **(settings.get("auto_rate", {}) or {})}, "自动评价配置查询成功")


@router.put("/{account_id}/auto-rate")
async def put_auto_rate(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    settings = await _save_action(account_id, user, db, {"auto_rate": payload or {}})
    return ok({"account_id": str(account_id), **(settings.get("auto_rate", {}) or {})}, "自动评价配置已保存")


@router.get("/{account_id}/refund-cancel")
async def get_refund_cancel(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    settings = await _save_action(account_id, user, db, {})
    return ok(settings.get("refund_cancel", {}), "退款订单注销配置查询成功")


@router.put("/{account_id}/refund-cancel")
async def put_refund_cancel(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    settings = await _save_action(account_id, user, db, {"refund_cancel": payload or {}})
    return ok(settings.get("refund_cancel", {}), "退款订单注销配置已保存")


@router.get("/{account_id}/confirm-receipt")
async def get_confirm_receipt(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    settings = await _save_action(account_id, user, db, {})
    return ok(settings.get("confirm_receipt", {}), "确认收货消息配置查询成功")


@router.put("/{account_id}/confirm-receipt")
async def put_confirm_receipt(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    settings = await _save_action(account_id, user, db, {"confirm_receipt": payload or {}})
    return ok(settings.get("confirm_receipt", {}), "确认收货消息配置已保存")


async def _save_upload(account_id: int, user: dict[str, Any], db: AsyncSession, file: UploadFile) -> str:
    await _account(account_id, user, db)
    upload_dir = Path(app_settings.static_dir) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "image.bin").name
    target = upload_dir / f"{uuid4().hex}_{safe_name}"
    target.write_bytes(await file.read())
    db.add(Upload(owner_id=_uid(user), filename=safe_name, path=str(target), mime_type=file.content_type, size=target.stat().st_size))
    await db.commit()
    return f"/static/uploads/{target.name}"


@router.post("/{account_id}/confirm-receipt/upload-image")
async def upload_confirm_receipt_image(
    account_id: int,
    image: UploadFile = File(...),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    image_url = await _save_upload(account_id, user, db, image)
    settings = await load_account_settings(db, _uid(user), account_id)
    await save_account_settings(db, _uid(user), account_id, {"confirm_receipt": {**(settings.get("confirm_receipt") or {}), "message_image": image_url}})
    return ok({"image_url": image_url}, "图片上传成功")


@router.post("/{account_id}/default-reply/upload-image")
async def upload_default_reply_image(
    account_id: int,
    image: UploadFile = File(...),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    image_url = await _save_upload(account_id, user, db, image)
    settings = await load_account_settings(db, _uid(user), account_id)
    await save_account_settings(db, _uid(user), account_id, {"default_reply": {**(settings.get("default_reply") or {}), "reply_image": image_url}})
    return ok({"image_url": image_url}, "图片上传成功")


@router.post("/{account_id}/ai-test")
async def test_ai(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    settings = await _save_action(account_id, user, db, {})
    ai_settings = settings.get("ai_settings") or {}
    try:
        reply = await test_ai_connection(
            ai_settings.get("provider_type"),
            ai_settings.get("base_url"),
            ai_settings.get("api_key"),
            ai_settings.get("model_name"),
        )
        return ok({"tested": True, "reply": reply}, "AI连接测试成功")
    except Exception as exc:
        # 保持旧前端的 success 外层结构，但测试失败必须明确返回 tested=false，
        # 不能再仅检查字段存在就提示“连接通过”。
        return ok({"tested": False}, f"AI连接测试失败：{str(exc)[:500]}")


@router.get("/{account_id}/proxy")
async def get_proxy(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    settings = await _save_action(account_id, user, db, {})
    return ok(settings.get("proxy_config", {"proxy_type": "none"}), "代理配置查询成功")


@router.put("/{account_id}/proxy")
async def put_proxy(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    values = payload or {}
    settings = await _save_action(account_id, user, db, {"proxy_config": values})
    return ok(settings.get("proxy_config", {"proxy_type": "none"}), "代理配置已保存")


@router.delete("/{account_id}/proxy")
async def delete_proxy(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    settings = await _save_action(account_id, user, db, {"proxy_config": {"proxy_type": "none"}})
    account = await _account(account_id, user, db)
    account.proxy = None
    await db.commit()
    return ok(settings.get("proxy_config", {"proxy_type": "none"}), "代理配置已清除")


@router.delete("/{account_id}")
async def delete_cookie_compat(account_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    """兼容旧前端删除路径，实际删除账号表中的账号。"""
    # 旧版页面实际调用的是 /cookies/{id}。必须执行统一的真实清理，
    # 否则页面会显示操作完成但账号仍然存在。
    return await delete_account(account_id, user, db)


@router.put("/{account_id}/auto-polish")
async def update_auto_polish(
    account_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    """保存自动擦亮开关；开启时立即执行当前账号一次。"""
    account = await _account(account_id, user, db)
    enabled = bool((payload or {}).get("auto_polish", False))
    account_settings = await save_account_settings(db, int(account.user_id), account_id, {"auto_polish": enabled})
    if not enabled:
        return ok({"account_id": account_id, "auto_polish": False, "immediate_run": None}, "商品自动擦亮已关闭")

    try:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{app_settings.scheduler_service_url.rstrip('/')}/api/v1/scheduled-tasks/refresh_listings/trigger",
                json={"account_id": account_id, "force": True},
            )
        response.raise_for_status()
        immediate_run = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # 开关已经保存，执行失败必须明确返回，避免界面误报“已完成”。
        raise HTTPException(status_code=503, detail=f"自动擦亮已开启，但立即执行失败：{str(exc)[:300]}") from exc

    run_data = immediate_run.get("data") if isinstance(immediate_run, dict) else {}
    if isinstance(run_data, dict) and run_data.get("total_items", 0) > 0 and run_data.get("fresh_success_count", 0) == 0 and run_data.get("already_polished_count", 0) > 0 and run_data.get("failed_count", 0) == 0:
        message = "商品自动擦亮已开启，但闲鱼未执行本次擦亮操作"
    else:
        message = "商品自动擦亮已开启，已立即执行一次"
    return ok(
        {"account_id": account_id, "auto_polish": True, "settings": account_settings, "immediate_run": immediate_run},
        message,
    )


@router.put("/{account_id}/{action}")
async def put_account_action(
    account_id: int,
    action: str,
    payload: dict[str, Any] | None = Body(default=None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
):
    field_map = {
        "auto-confirm": "auto_confirm",
        "pause-duration": "pause_duration",
        "message-expire-time": "message_expire_time",
        "reply-delay": "reply_delay_seconds",
        "scheduled-redelivery": "scheduled_redelivery",
        "scheduled-rate": "scheduled_rate",
        "auto-polish": "auto_polish",
        "confirm-before-send": "confirm_before_send",
        "send-before-confirm": "send_before_confirm",
        "only-send-card": "only_send_card",
        "auto-red-flower": "auto_red_flower",
        "ai-reply-block-ordered-users": "ai_reply_block_ordered_users",
        "delivery-disabled": "delivery_disabled",
        "login-info": "login_info",
    }
    field = field_map.get(action)
    if field is None:
        raise HTTPException(status_code=404, detail="不支持的账号操作")
    values = payload or {}
    if field == "login_info":
        settings = await _save_action(account_id, user, db, values)
    else:
        value_key = next((key for key in (field, "reply_delay_seconds", "pause_duration", "message_expire_time") if key in values), field)
        settings = await _save_action(account_id, user, db, {field: values.get(value_key)})
    return ok(settings, "账号设置已更新")
