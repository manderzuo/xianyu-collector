# -*- coding: utf-8 -*-
"""SQLAlchemy 2.0 声明式基类。"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的公共基类。

    子类只需声明 __tablename__ 与字段即可,
    create_all / Alembic 均基于 Base.metadata 工作。
    """
