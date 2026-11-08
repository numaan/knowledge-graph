"""Metadata tables (SQLAlchemy Core). PostgreSQL in production, SQLite in tests."""
import logging
import os
import uuid

from sqlalchemy import (JSON, Boolean, Column, DateTime, ForeignKey, Index, MetaData, String, Table, Text, create_engine,
                        func, inspect, select)
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

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

# At most one active run for each source. The route and the sensor check first; this index closes the race.
ACTIVE_RUN_INDEX = Index(
    "uq_runs_one_active_per_source", runs.c.source_id, unique=True,
    sqlite_where=runs.c.status.in_(("queued", "running")), postgresql_where=runs.c.status.in_(("queued", "running")))

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
    """Idempotent. Also upgrades a database that was created before the active-run index."""
    metadata.create_all(engine)
    insp = inspect(engine)
    if ACTIVE_RUN_INDEX.name in {i["name"] for i in insp.get_indexes("runs")}:
        return
    with engine.connect() as conn:
        dup = conn.execute(select(runs.c.source_id).where(
            runs.c.source_id.is_not(None), runs.c.status.in_(("queued", "running")))
            .group_by(runs.c.source_id).having(func.count() > 1)).first()
    if dup is not None:
        log.warning("runs has several active rows for source %s; the unique active-run index is not created. "
                    "Cancel the extra runs and restart.", dup.source_id)
        return
    ACTIVE_RUN_INDEX.create(engine, checkfirst=True)
