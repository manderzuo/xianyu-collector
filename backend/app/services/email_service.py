# -*- coding: utf-8 -*-
"""系统邮件发送服务。

验证码和通知邮件共用数据库中的 ``smtp_*`` 设置。未完成配置时明确报错，
不在接口层伪造“发送成功”。
"""
from __future__ import annotations

import asyncio
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.system import SystemSetting


def _smtp_value(values: dict[str, str], name: str, default: str = "") -> str:
    return str(values.get(name, default) or default).strip()


async def get_smtp_settings(db: AsyncSession) -> dict[str, str]:
    rows = (
        await db.execute(
            select(SystemSetting).where(SystemSetting.setting_key.like("smtp_%"))
        )
    ).scalars().all()
    return {
        str(row.setting_key)[5:]: str(row.setting_value or "")
        for row in rows
    }


def _send_smtp_sync(
    *,
    to_email: str,
    subject: str,
    content: str,
    config: dict[str, str],
) -> None:
    server = _smtp_value(config, "server")
    username = _smtp_value(config, "user")
    password = _smtp_value(config, "password")
    sender = _smtp_value(config, "from", username) or username
    if not server or not username or not password:
        raise RuntimeError("邮件SMTP配置不完整，请先在系统设置中配置服务器、账号和授权码")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to_email):
        raise RuntimeError("收件邮箱格式不正确")
    try:
        port = int(_smtp_value(config, "port", "587"))
    except ValueError as exc:
        raise RuntimeError("SMTP端口必须是数字") from exc
    use_ssl = _smtp_value(config, "use_ssl").lower() in {"1", "true", "yes", "on"}
    use_tls = _smtp_value(config, "use_tls", "true").lower() in {"1", "true", "yes", "on"}

    message = MIMEMultipart("alternative")
    message["From"] = formataddr(("系统通知", sender))
    message["To"] = to_email
    message["Subject"] = subject
    message.attach(MIMEText(content, "plain", "utf-8"))

    smtp: smtplib.SMTP | smtplib.SMTP_SSL | None = None
    try:
        smtp = smtplib.SMTP_SSL(server, port, timeout=15) if use_ssl else smtplib.SMTP(server, port, timeout=15)
        smtp.ehlo()
        if use_tls and not use_ssl:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(username, password)
        smtp.sendmail(sender, [to_email], message.as_string())
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError("SMTP认证失败，请检查账号及邮箱授权码") from exc
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, OSError, TimeoutError) as exc:
        raise RuntimeError(f"SMTP服务器连接失败：{str(exc)[:180]}") from exc
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                try:
                    smtp.close()
                except Exception:
                    pass


async def send_email(
    db: AsyncSession,
    *,
    to_email: str,
    subject: str,
    content: str,
) -> None:
    config = await get_smtp_settings(db)
    await asyncio.to_thread(
        _send_smtp_sync,
        to_email=to_email,
        subject=subject,
        content=content,
        config=config,
    )


async def send_verification_code_email(
    db: AsyncSession,
    *,
    to_email: str,
    code: str,
    code_type: str,
) -> None:
    action = {
        "login": "登录账号",
        "reset_password": "重置密码",
    }.get(code_type, "安全操作")
    await send_email(
        db,
        to_email=to_email,
        subject="系统验证码",
        content=(
            f"您正在{action}，本次验证码为：{code}\n\n"
            "验证码有效期为5分钟，请勿将验证码转发给他人。"
        ),
    )


def smtp_config_for_test(values: dict[str, Any]) -> dict[str, str]:
    """把测试接口传入的配置规范化为邮件发送服务使用的键名。"""
    return {str(key): str(value or "") for key, value in values.items()}
