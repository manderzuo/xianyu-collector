# -*- coding: utf-8 -*-
"""把本机用户表和邀请码镜像到云端统一认证服务。

背景：1.3.5 起登录改由云端做密码权威，但界面注册只写本机 MySQL，
云端从来没有这些账号，导致「本机注册成功、登录却提示密码错误」。
本模块负责把存量账号和邀请码无损同步到云端。

两侧密码哈希的 KDF 参数完全相同（PBKDF2-HMAC-SHA256 / 120000 次），
只是序列化格式不同，因此可以在不知道明文密码的前提下无损转码。
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models import RegistrationInvite, User
from common.services.cloud_auth import CloudAuthError, cloud_auth_request, cloud_auth_url

logger = logging.getLogger("xr.cloud_sync")

#: 与部署端一致：两侧都用同样的 PBKDF2 参数，转码后云端可以正常校验。
LOCAL_HASH_SCHEME = "pbkdf2"
CLOUD_HASH_SCHEME = "pbkdf2_sha256"
CLOUD_HASH_ITERATIONS = 120_000

#: 单次导入批量上限，与云端 MAX_IMPORT_BATCH 一致。
IMPORT_BATCH_SIZE = 500

#: 本机 user.status（int）→ 云端状态（str）。
STATUS_TO_CLOUD = {1: "approved", 2: "pending", 0: "disabled"}


def local_hash_to_cloud(value: Any) -> str | None:
    """把本机密码哈希转成云端格式；无法解析时返回 None。

    本机：``pbkdf2$<salt>$$<hex digest>``（salt 为明文串）
    云端：``pbkdf2_sha256$<iterations>$<b64url salt>$<b64url digest>``

    两者使用同一 KDF 与迭代次数，所以 salt 与 digest 可以直接搬运，
    不需要明文密码。已经是云端格式的哈希原样返回，便于重复执行。
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith(f"{CLOUD_HASH_SCHEME}$"):
        return text
    parts = text.split("$")
    if len(parts) != 3 or parts[0] != LOCAL_HASH_SCHEME:
        return None
    salt_text, digest_hex = parts[1], parts[2]
    if not salt_text or not digest_hex:
        return None
    try:
        digest_bytes = bytes.fromhex(digest_hex)
    except ValueError:
        return None
    salt_b64 = base64.urlsafe_b64encode(salt_text.encode("utf-8")).decode("ascii")
    digest_b64 = base64.urlsafe_b64encode(digest_bytes).decode("ascii")
    return f"{CLOUD_HASH_SCHEME}${CLOUD_HASH_ITERATIONS}${salt_b64}${digest_b64}"


def cloud_status(value: Any) -> str:
    try:
        return STATUS_TO_CLOUD.get(int(value), "disabled")
    except (TypeError, ValueError):
        return "disabled"


def cloud_role(value: Any) -> str:
    return "admin" if str(value or "").strip().lower() in {"admin", "administrator"} else "employee"


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def build_user_payload(users: list[User]) -> tuple[list[dict[str, Any]], list[str]]:
    """构造云端导入负载，返回 (可导入项, 被跳过的用户名)。"""
    items: list[dict[str, Any]] = []
    skipped: list[str] = []
    for user in users:
        username = str(user.username or "").strip()
        if not username:
            skipped.append("(空用户名)")
            continue
        password_hash = local_hash_to_cloud(user.password_hash)
        if password_hash is None:
            # 哈希无法解析的账号不能导入：宁可跳过也不能写入无法验证的哈希，
            # 否则该账号在云端会变成永远登不上且无法自愈的死号。
            skipped.append(username)
            continue
        items.append({
            "username": username,
            "password_hash": password_hash,
            "employee_name": str(user.nickname or username).strip()[:80],
            "role": cloud_role(user.role),
            "status": cloud_status(user.status),
            "email": str(user.email or "").strip().lower() or None,
            "plan_code": str(user.plan_code or "NORMAL").strip().upper()[:32] or "NORMAL",
            "created_at": _isoformat(user.created_at),
        })
    return items, skipped


def build_invite_payload(invites: list[RegistrationInvite]) -> tuple[list[dict[str, Any]], list[str]]:
    """构造邀请码同步负载。

    本机只保存哈希与预览，明文没有落库（``code_encrypted`` 一直为空），
    因此按哈希同步。两侧哈希算法一致，云端校验行为与新建邀请码完全相同。
    """
    items: list[dict[str, Any]] = []
    skipped: list[str] = []
    for invite in invites:
        code_hash = str(invite.code_hash or "").strip().lower()
        if len(code_hash) != 64:
            skipped.append(str(invite.id))
            continue
        items.append({
            "code_hash": code_hash,
            "code_preview": str(invite.code_preview or "").strip()[:64],
            "status": str(invite.status or "active").strip().lower(),
            "expires_at": _isoformat(invite.expires_at),
        })
    return items, skipped


def chunked(items: list[dict[str, Any]], size: int = IMPORT_BATCH_SIZE) -> list[list[dict[str, Any]]]:
    """按 size 切分，保证单次请求不超过云端批量上限。"""
    return [items[index:index + size] for index in range(0, len(items), size)]


async def sync_users_to_cloud(
    session: AsyncSession,
    *,
    token: str = "",
    sync_secret: str = "",
) -> dict[str, Any]:
    """把本机用户表里的存量账号导入云端；只创建云端缺失的账号。

    云端是权限与密码的权威来源，因此这里**从不覆盖**云端已有记录。
    """
    if not cloud_auth_url():
        return {"skipped": True, "reason": "cloud_auth_disabled"}
    users = list((await session.execute(select(User).order_by(User.id.asc()))).scalars().all())
    if not users:
        return {"skipped": True, "reason": "no_local_users"}

    items, unreadable = build_user_payload(users)
    if not items:
        return {"skipped": True, "reason": "no_importable_users", "unreadable": unreadable}

    created = 0
    already = 0
    for batch in chunked(items):
        payload: dict[str, Any] = {"users": batch}
        if sync_secret and not token:
            payload["sync_secret"] = sync_secret
        try:
            result = await cloud_auth_request("sync_users", payload, token)
        except CloudAuthError as exc:
            logger.warning("cloud user import failed code=%s status=%s message=%s", exc.code, exc.status_code, exc)
            return {"skipped": True, "reason": exc.code, "created": created, "unreadable": unreadable}
        created += int((result or {}).get("created") or 0)
        already += int((result or {}).get("skipped") or 0)

    logger.info(
        "cloud user import finished local=%s importable=%s created=%s already_present=%s unreadable=%s",
        len(users), len(items), created, already, len(unreadable),
    )
    return {"created": created, "already_present": already, "unreadable": unreadable}


async def sync_invites_to_cloud(session: AsyncSession, *, token: str = "") -> dict[str, Any]:
    """把本机邀请码同步到云端，使任一机器注册时都能通过校验。

    需要管理员云端会话令牌：云端把 sync_invites 放在管理员门禁之后。
    """
    if not cloud_auth_url() or not token:
        return {"skipped": True, "reason": "cloud_auth_disabled_or_no_token"}
    invites = list((await session.execute(select(RegistrationInvite).order_by(RegistrationInvite.id.asc()))).scalars().all())
    if not invites:
        return {"skipped": True, "reason": "no_local_invites"}

    items, unreadable = build_invite_payload(invites)
    if not items:
        return {"skipped": True, "reason": "no_syncable_invites", "unreadable": unreadable}

    synced = 0
    for batch in chunked(items, 100):
        try:
            await cloud_auth_request("sync_invites", {"items": batch}, token)
        except CloudAuthError as exc:
            logger.warning("cloud invite sync failed code=%s status=%s message=%s", exc.code, exc.status_code, exc)
            return {"skipped": True, "reason": exc.code, "synced": synced}
        synced += len(batch)

    logger.info("cloud invite sync finished local=%s synced=%s unreadable=%s", len(invites), synced, len(unreadable))
    return {"synced": synced, "unreadable": unreadable}
