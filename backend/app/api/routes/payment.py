# -*- coding: utf-8 -*-
"""充值、提现及结算查询接口。

充值只在部署了收款码/支付回调后创建待支付单；提现先冻结余额并落库，
避免旧兼容接口把“已登记”误报成“已支付”。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import html
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.dependencies import get_current_user
from backend.app.core.response import ok
from backend.app.core.security import decode_access_token
from common.config import settings
from common.db.session import get_session
from common.models import FeatureRecord, PaymentRecord, SystemSetting, User

router = APIRouter(prefix="/api/v1/payment", tags=["资金与支付"])


def _uid(user: dict) -> int:
    try:
        return int(user.get("sub", 1))
    except (TypeError, ValueError):
        return 1


def _admin(user: dict) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator"} or bool(user.get("is_admin"))


def _review_token(record_id: int, action: str) -> str:
    return hmac.new(
        settings.jwt_secret.encode(),
        f"{record_id}:{action}".encode(),
        hashlib.sha256,
    ).hexdigest()[:32]


def _valid_review_token(record_id: int, action: str, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(_review_token(record_id, action), token)


def _review_error(message: str, status_code: int = 400) -> HTMLResponse:
    safe = html.escape(message)
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>提现审核</title></head>"
        f"<body style='font-family:sans-serif;text-align:center;padding:60px;color:#ef4444'><h2>{safe}</h2></body></html>",
        status_code=status_code,
    )


def _review_page(record_id: int, action: str, token: str) -> str:
    is_approve = action == "approve"
    title = "通过提现申请" if is_approve else "拒绝提现申请"
    button = "确认通过" if is_approve else "确认拒绝"
    color = "#10b981" if is_approve else "#ef4444"
    reason = "" if is_approve else (
        "<label style='display:block;text-align:left;font-size:14px;color:#374151;margin:0 0 6px'>"
        "拒绝原因（可选）</label><textarea id='reason' rows='4' placeholder='请输入拒绝原因，将通知用户...' "
        "style='width:100%;box-sizing:border-box;padding:10px;border:1px solid #d1d5db;border-radius:8px;resize:vertical'></textarea>"
    )
    endpoint = "/api/v1/payment/withdraw/approve" if is_approve else "/api/v1/payment/withdraw/reject"
    reason_body = "" if is_approve else "body.append('reject_reason', document.getElementById('reason').value);"
    return f"""<!doctype html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{title}</title>
<style>body{{font-family:sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#f4f7fa}}.card{{background:#fff;border-radius:16px;padding:40px;box-shadow:0 4px 24px rgba(0,0,0,.08);max-width:440px;width:90%}}button{{flex:1;padding:12px;border:0;border-radius:8px;font-size:15px;cursor:pointer}}.row{{display:flex;gap:12px;margin-top:20px}}</style>
</head><body><div class='card'><h2 style='text-align:center;color:#1a1a2e'>{title} #{record_id}</h2>
<div id='form-area'>{reason}<div id='error' style='display:none;color:#ef4444;font-size:13px;margin-top:12px;text-align:center'></div>
<div class='row'><button id='cancel' onclick='history.back()' style='background:#f3f4f6;color:#374151'>取消</button>
<button id='submit' onclick='submitReview()' style='background:{color};color:#fff;font-weight:600'>{button}</button></div></div>
<div id='result' style='display:none;text-align:center'><div id='icon' style='font-size:48px'>✅</div><h3 id='message'></h3></div>
<script>
function submitReview() {{
  const submit = document.getElementById('submit'); const cancel = document.getElementById('cancel');
  const error = document.getElementById('error'); error.style.display = 'none'; submit.disabled = true; cancel.disabled = true; submit.textContent = '处理中...';
  const body = new URLSearchParams(); body.append('id', '{record_id}'); body.append('token', '{token}'); {reason_body}
  fetch('{endpoint}', {{method:'POST', headers:{{'Content-Type':'application/x-www-form-urlencoded'}}, body:body.toString()}})
    .then(async response => {{ const data = await response.json(); if (!response.ok) throw new Error(data.detail || data.message || '操作失败'); return data; }})
    .then(data => {{ document.getElementById('form-area').style.display='none'; document.getElementById('result').style.display='block'; document.getElementById('message').textContent=data.message || '操作成功'; }})
    .catch(errorValue => {{ submit.disabled=false; cancel.disabled=false; submit.textContent='{button}'; error.textContent=errorValue.message || '网络错误，请重试'; error.style.display='block'; }});
}}
</script></div></body></html>"""


async def _apply_review(db: AsyncSession, record_id: int, action: str, reject_reason: str = "") -> dict:
    row = (await db.execute(
        select(FeatureRecord).where(
            FeatureRecord.id == record_id,
            FeatureRecord.feature == "settlement-records",
        ).with_for_update()
    )).scalar_one_or_none()
    if row is None:
        return {"success": False, "message": "结算记录不存在"}
    current = str((row.payload or {}).get("status") or row.status or "")
    if current != "pending_review":
        labels = {"approved": "已通过", "rejected": "已拒绝", "paid": "已打款"}
        return {"success": False, "message": f"该记录已处理，当前状态：{labels.get(current, current)}"}
    value = dict(row.payload or {})
    status = "approved" if action == "approve" else "rejected"
    value["status"] = status
    if action == "approve":
        value["remark"] = f"{value.get('remark') or ''}\n管理员已通过审核".strip()
        message = "提现申请已通过审核"
    else:
        value["remark"] = f"{value.get('remark') or ''}\n管理员已拒绝审核".strip()
        if reject_reason.strip(): value["reject_reason"] = reject_reason.strip()
        message = "提现申请已拒绝"
    row.payload = value
    row.status = status
    await db.commit()
    return {"success": True, "message": message, "data": {"id": row.id, "status": status, "amount": value.get("amount")}}


async def _setting(db: AsyncSession, key: str) -> str:
    value = (await db.execute(select(SystemSetting.setting_value).where(SystemSetting.setting_key == key).limit(1))).scalar_one_or_none()
    return str(value or "").strip()


def _money(value: object, *, allow_zero: bool = False) -> Decimal:
    try:
        amount = Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(422, "金额格式无效") from exc
    if amount < 0 or (not allow_zero and amount <= 0):
        raise HTTPException(422, "金额必须大于 0")
    return amount


def _payment(row: PaymentRecord, order_no: str | None = None) -> dict:
    return {
        "order_id": row.id, "order_no": order_no or row.external_ref or str(row.id),
        "amount": f"{Decimal(str(row.amount or 0)):.2f}", "status": row.status,
        "trade_no": row.external_ref if row.provider == "alipay" and row.status == "paid" else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _settlement(row: FeatureRecord) -> dict:
    value = dict(row.payload or {})
    return {
        "id": row.id, "alipay_id": value.get("alipay_id"), "payment_type": value.get("payment_type"),
        "payment_qrcode": value.get("payment_qrcode"), "amount": str(value.get("amount") or "0.00"),
        "status": str(value.get("status") or row.status), "remark": value.get("remark"),
        "reject_reason": value.get("reject_reason"),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _user_setting(db: AsyncSession, user_id: int, key: str) -> str:
    row = (await db.execute(
        select(FeatureRecord).where(FeatureRecord.owner_id == user_id, FeatureRecord.feature == f"user-setting:{key}").order_by(desc(FeatureRecord.id)).limit(1)
    )).scalar_one_or_none()
    return str((row.payload or {}).get("value") or "").strip() if row else ""


async def _write_flow(db: AsyncSession, user_id: int, *, flow_type: str, amount: Decimal, before: Decimal, after: Decimal, description: str, order_id: str = "") -> None:
    db.add(FeatureRecord(
        owner_id=user_id, feature="fund-flows", external_id=uuid4().hex, status="posted",
        payload={"user_id": user_id, "type": flow_type, "amount": f"{amount:.2f}", "balance_before": f"{before:.2f}", "balance_after": f"{after:.2f}", "order_id": order_id, "description": description},
        note="资金流水",
    ))


@router.post("/recharge")
async def create_recharge(payload: dict | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    amount = _money((payload or {}).get("amount"))
    qr_code = await _setting(db, "payment.recharge_qrcode")
    if not qr_code:
        raise HTTPException(503, "充值支付通道尚未配置，请管理员先配置 payment.recharge_qrcode")
    order_no = f"XR-R-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:10].upper()}"
    row = PaymentRecord(owner_id=_uid(user), provider="manual", amount=float(amount), status="pending", external_ref=order_no)
    db.add(row); await db.commit(); await db.refresh(row)
    return ok({**_payment(row, order_no), "qr_code": qr_code}, "充值订单已创建，请扫码支付")


@router.get("/recharge/{order_no}")
async def get_recharge(order_no: str, user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    statement = select(PaymentRecord).where(PaymentRecord.external_ref == order_no)
    if not _admin(user): statement = statement.where(PaymentRecord.owner_id == _uid(user))
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None: raise HTTPException(404, "充值订单不存在")
    return ok(_payment(row, order_no), "充值订单查询成功")


@router.post("/alipay/notify")
async def alipay_notify(request: Request, db: AsyncSession = Depends(get_session)):
    del request, db
    raise HTTPException(503, "支付宝回调验签服务尚未配置，未修改任何余额")


@router.post("/withdraw")
async def create_withdraw(payload: dict | None = Body(default=None), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    user_id = _uid(user)
    payment_qrcode = await _user_setting(db, user_id, "payment_qrcode")
    if not payment_qrcode:
        raise HTTPException(422, "请先上传收款码")
    amount = _money((payload or {}).get("amount"))
    minimum = await _setting(db, "withdraw.min_amount")
    if minimum:
        try:
            if amount < Decimal(minimum): raise HTTPException(422, f"提现金额不能低于 ¥{Decimal(minimum):.2f}")
        except InvalidOperation as exc:
            raise HTTPException(422, "最低提现金额配置无效") from exc
    account = (await db.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one_or_none()
    if account is None: raise HTTPException(404, "用户不存在")
    before = Decimal(str(account.balance or 0)).quantize(Decimal("0.01"))
    if amount > before: raise HTTPException(422, "提现金额不能超过当前余额")
    after = before - amount; account.balance = after
    payment_type = await _user_setting(db, user_id, "payment_type") or "alipay"
    row = FeatureRecord(owner_id=user_id, feature="settlement-records", external_id=f"withdraw:{uuid4().hex}", status="pending_review", payload={"amount": f"{amount:.2f}", "status": "pending_review", "payment_type": payment_type, "payment_qrcode": payment_qrcode, "remark": "", "reject_reason": ""}, note="提现申请")
    db.add(row); await _write_flow(db, user_id, flow_type="expense", amount=amount, before=before, after=after, description="提现申请，等待审核", order_id=f"withdraw:{uuid4().hex[:12]}")
    await db.commit(); await db.refresh(row)
    return ok({"id": row.id, "amount": f"{amount:.2f}", "status": "pending_review", "balance": f"{after:.2f}", "alipay_id": "", "created_at": row.created_at.isoformat() if row.created_at else None}, "提现申请已提交，等待审核")


@router.get("/settlement-records")
async def settlement_records(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), user=Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    statement = select(FeatureRecord).where(FeatureRecord.feature == "settlement-records")
    if not _admin(user): statement = statement.where(FeatureRecord.owner_id == _uid(user))
    rows = list((await db.execute(statement.order_by(desc(FeatureRecord.id)))).scalars().all())
    values = rows[(page - 1) * page_size: page * page_size]
    return ok({"list": [_settlement(row) for row in values], "total": len(rows), "page": page, "page_size": page_size, "total_pages": (len(rows) + page_size - 1) // page_size}, "结算记录查询成功")


@router.get("/withdraw/review")
async def withdraw_review(
    request: Request,
    id: int | None = Query(None),
    action: str | None = Query(None),
    token: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_session),
):
    """提现审核邮件入口和管理员列表共用的兼容路由。

    带 id/action/token 的邮件链接只展示二次确认页，真正变更必须由 POST
    审核接口完成，避免邮件客户端预取链接导致误审核。
    """
    if id is not None or action is not None or token is not None:
        if id is None or action not in {"approve", "reject"} or not _valid_review_token(id, action, token):
            return _review_error("无效的审核令牌")
        return HTMLResponse(_review_page(id, action, token or ""))

    authorization = request.headers.get("authorization", "")
    user = None
    if authorization.lower().startswith("bearer "):
        user = decode_access_token(authorization[7:].strip())
    if user is None and settings.environment != "production":
        user = {"sub": "1", "username": "admin", "role": "admin"}
    if not user:
        raise HTTPException(401, "请先登录")
    if not _admin(user):
        raise HTTPException(403, "仅管理员可以审核提现")
    rows = list((await db.execute(select(FeatureRecord).where(FeatureRecord.feature == "settlement-records").order_by(desc(FeatureRecord.id)))).scalars().all())
    values = rows[(page - 1) * page_size: page * page_size]
    return ok({"list": [_settlement(row) for row in values], "total": len(rows), "page": page, "page_size": page_size}, "提现记录查询成功")


async def _review_post_payload(request: Request) -> dict:
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
        return payload if isinstance(payload, dict) else {}
    form = await request.form()
    return dict(form)


@router.post("/withdraw/approve")
async def approve_withdraw(request: Request, db: AsyncSession = Depends(get_session)):
    payload = await _review_post_payload(request)
    try:
        record_id = int(payload.get("id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(422, "提现记录ID无效")
    token = str(payload.get("token") or "")
    if not _valid_review_token(record_id, "approve", token):
        return JSONResponse({"success": False, "message": "无效的审核令牌"}, status_code=400)
    return JSONResponse(await _apply_review(db, record_id, "approve"))


@router.post("/withdraw/reject")
async def reject_withdraw(request: Request, db: AsyncSession = Depends(get_session)):
    payload = await _review_post_payload(request)
    try:
        record_id = int(payload.get("id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(422, "提现记录ID无效")
    token = str(payload.get("token") or "")
    if not _valid_review_token(record_id, "reject", token):
        return JSONResponse({"success": False, "message": "无效的审核令牌"}, status_code=400)
    return JSONResponse(await _apply_review(db, record_id, "reject", str(payload.get("reject_reason") or "")))
