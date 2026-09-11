# -*- coding: utf-8 -*-
"""异步数据库会话管理(SQLAlchemy 2.0 + asyncmy)。

连接参数统一读取环境变量 MYSQL_HOST / MYSQL_PORT / MYSQL_USER /
MYSQL_PASSWORD / MYSQL_DATABASE(经 common.config.Settings 汇总)。
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import inspect as sqlalchemy_inspect, text
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError

from common.config import settings
from common.db.base import Base

# 模块级异步引擎:进程内复用同一连接池
async_engine = create_async_engine(
    settings.mysql_dsn,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=(settings.environment == "development"),
)

# 异步会话工厂:每个请求/任务创建短生命周期会话
async_session_maker = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖项:为一次请求提供一个数据库会话,自动关闭。"""
    try:
        async with async_session_maker() as session:
            yield session
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="本机数据库连接失败，请重新启动应用或检查 Docker 数据库配置",
        ) from exc


async def init_db() -> None:
    """首次启动创建表结构。

    模型集中导入的副作用会把全部表注册到 ``Base.metadata``。生产环境后续
    可替换为 Alembic；当前框架启动必须具备可重复的最小初始化能力。
    """
    import common.models  # noqa: F401

    async with async_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_migrate_user_columns)
        await connection.run_sync(_migrate_account_columns)
        await connection.run_sync(_migrate_order_columns)
        await connection.run_sync(_migrate_scheduled_task_columns)
        await connection.run_sync(_migrate_registration_invite_columns)
        await connection.run_sync(_migrate_entitlements_v1)

    # 默认套餐采用幂等种子数据；只补缺失套餐/功能，不覆盖管理员已配置的值。
    from common.services.entitlements import ensure_default_plans
    async with async_session_maker() as session:
        await ensure_default_plans(session)


def _migrate_user_columns(connection) -> None:
    """给 create_all 无法更新的旧用户表补齐新增字段。

    项目当前没有运行时迁移框架，而用户管理接口需要这些字段承载账号
    限额、余额和到期时间。只对缺失列执行一次 ALTER，不覆盖已有数据。
    """
    inspector = sqlalchemy_inspect(connection)
    if "xr_users" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("xr_users")}
    missing = {
        "phone": "VARCHAR(32) NULL",
        "account_limit": "INT NULL",
        "balance": "DECIMAL(18, 2) NOT NULL DEFAULT 0",
        "expire_at": "DATETIME NULL",
        "plan_code": "VARCHAR(32) NOT NULL DEFAULT 'NORMAL'",
        "plan_expires_at": "DATETIME NULL",
        "auth_version": "INT NOT NULL DEFAULT 1",
    }
    for name, definition in missing.items():
        if name not in existing:
            connection.execute(text(f"ALTER TABLE xr_users ADD COLUMN {name} {definition}"))


def _migrate_entitlements_v1(connection) -> None:
    """记录权限模型首个版本，便于后续迁移和发布检查。"""
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS xr_schema_migrations ("
        "version VARCHAR(64) PRIMARY KEY, "
        "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    ))
    exists = connection.execute(
        text("SELECT version FROM xr_schema_migrations WHERE version = :version"),
        {"version": "20260904_entitlements_v1"},
    ).first()
    if exists is None:
        connection.execute(
            text("INSERT INTO xr_schema_migrations(version) VALUES (:version)"),
            {"version": "20260904_entitlements_v1"},
        )


def _migrate_account_columns(connection) -> None:
    """给旧闲鱼账号表补齐设备指纹字段。

    ``im_device_id`` 用于把 IM 设备指纹固定下来；旧库可能没有该列，
    缺失时补一列可空字段，由运行时首次取 Token 时写入。
    """
    inspector = sqlalchemy_inspect(connection)
    if "xr_accounts" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("xr_accounts")}
    if "im_device_id" not in existing:
        connection.execute(text("ALTER TABLE xr_accounts ADD COLUMN im_device_id VARCHAR(128) NULL"))


def _migrate_order_columns(connection) -> None:
    """给已有订单表补齐卡券发货和商品规格字段。"""
    inspector = sqlalchemy_inspect(connection)
    if "xr_orders" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("xr_orders")}
    missing = {
        "buyer_nick": "VARCHAR(255) NULL",
        "item_external_id": "VARCHAR(128) NULL",
        "item_title": "VARCHAR(255) NULL",
        "quantity": "INT NOT NULL DEFAULT 1",
        "spec_name": "VARCHAR(128) NULL",
        "spec_value": "VARCHAR(255) NULL",
        "payload": "JSON NULL",
        "is_rated": "TINYINT(1) NOT NULL DEFAULT 0",
        "is_red_flower": "TINYINT(1) NOT NULL DEFAULT 0",
        "placed_at": "DATETIME NULL",
        "delivery_method": "VARCHAR(16) NULL",
        "delivery_content": "TEXT NULL",
        "delivery_fail_reason": "TEXT NULL",
        "delivery_send_status": "VARCHAR(16) NULL",
        "delivery_send_fail_reason": "TEXT NULL",
        "card_only_delivered": "TINYINT(1) NOT NULL DEFAULT 0",
        "delivered_at": "DATETIME NULL",
    }
    for name, definition in missing.items():
        if name not in existing:
            connection.execute(text(f"ALTER TABLE xr_orders ADD COLUMN {name} {definition}"))


def _migrate_scheduled_task_columns(connection) -> None:
    """给已有调度任务表补齐页面可编辑的执行间隔字段。"""
    inspector = sqlalchemy_inspect(connection)
    if "xr_scheduled_tasks" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("xr_scheduled_tasks")}
    if "interval_seconds" not in existing:
        connection.execute(text("ALTER TABLE xr_scheduled_tasks ADD COLUMN interval_seconds INT NULL"))


def _migrate_registration_invite_columns(connection) -> None:
    """给已有邀请码表补齐加密存储列。"""
    inspector = sqlalchemy_inspect(connection)
    if "xr_registration_invites" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("xr_registration_invites")}
    if "code_encrypted" not in existing:
        connection.execute(text("ALTER TABLE xr_registration_invites ADD COLUMN code_encrypted TEXT NULL"))
