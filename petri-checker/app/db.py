"""PostgreSQL 持久化：模型版本、检查运行、发布记录。"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Boolean, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATABASE_URL = os.environ.get(
    "PETRI_DATABASE_URL",
    "postgresql+psycopg://postgres@127.0.0.1:54329/petri",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ModelRow(Base):
    __tablename__ = "models"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), index=True)
    version: Mapped[int] = mapped_column(Integer)          # 每次更新 +1
    spec: Mapped[dict] = mapped_column(JSONB)
    spec_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CheckRunRow(Base):
    __tablename__ = "check_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id"), index=True)
    model_version: Mapped[int] = mapped_column(Integer)
    spec_hash: Mapped[str] = mapped_column(String(64), index=True)  # 结论绑定的规范指纹
    params: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))        # completed | truncated
    conclusive: Mapped[bool] = mapped_column(Boolean)
    summary: Mapped[dict] = mapped_column(JSONB)
    findings: Mapped[list] = mapped_column(JSONB)
    frontier: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PublicationRow(Base):
    __tablename__ = "publications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id"), index=True)
    check_run_id: Mapped[str] = mapped_column(ForeignKey("check_runs.id"))
    model_version: Mapped[int] = mapped_column(Integer)
    spec_hash: Mapped[str] = mapped_column(String(64))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def init_db() -> None:
    Base.metadata.create_all(engine)
