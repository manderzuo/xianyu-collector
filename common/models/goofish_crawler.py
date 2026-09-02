"""Goofish 搜索采集任务和结果。"""
from datetime import datetime
from sqlalchemy import JSON, BigInteger, DateTime, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base import Base


class GoofishCrawlJob(Base):
    __tablename__ = "xr_goofish_crawl_jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    account_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    keyword: Mapped[str] = mapped_column(String(255), nullable=False)
    interval_seconds: Mapped[int] = mapped_column(default=900, nullable=False)
    start_page: Mapped[int] = mapped_column(default=1, nullable=False)
    pages: Mapped[int] = mapped_column(default=1, nullable=False)
    page_size: Mapped[int] = mapped_column(default=20, nullable=False)
    enabled: Mapped[int] = mapped_column(default=1, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class GoofishCrawlResult(Base):
    __tablename__ = "xr_goofish_crawl_results"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    seller: Mapped[str | None] = mapped_column(String(128))
    area: Mapped[str | None] = mapped_column(String(128))
    url: Mapped[str | None] = mapped_column(String(512))
    payload: Mapped[dict | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
