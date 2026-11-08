"""Metadata tables (SQLAlchemy Core). PostgreSQL in production, SQLite in tests."""
import os
import uuid

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, MetaData, String, Table, Text, create_engine
from sqlalchemy.engine import Engine

metadata = MetaData()

sources = Table(
    "sources", metadata,
    Column("id", String, primary_key=True),
    Column("kind", String, nullable=False),
    Column("name", String, nullable=False, unique=True),
    Column("config", JSON, nullable=False),
    Column("secret", Text, nullable=True),
    Column("schedule", String, nullable=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("origin", String, nullable=False, default="ui"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("created_by", String, nullable=True),
)

runs = Table(
    "runs", metadata,
    Column("id", String, primary_key=True),
    Column("source_id", String, ForeignKey("sources.id", ondelete="SET NULL"), nullable=True),
    Column("source_name", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("status", String, nullable=False),
    Column("trigger", String, nullable=False),
    Column("mode", String, nullable=False),
    Column("dagster_run_id", String, nullable=True),
    Column("stats", JSON, nullable=True),
    Column("error", Text, nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("requested_by", String, nullable=True),
)

audit = Table(
    "audit", metadata,
    Column("id", String, primary_key=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("actor", String, nullable=False),
    Column("action", String, nullable=False),
    Column("target", String, nullable=True),
    Column("detail", JSON, nullable=True),
)


def new_id(prefix: str) -> str:
    """`src_` / `run_` / `aud_` plus 12 hex characters."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def get_engine(url: str | None = None) -> Engine:
    url = url or os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return create_engine(url, future=True)


def create_all(engine: Engine) -> None:
    """Idempotent."""
    metadata.create_all(engine)
