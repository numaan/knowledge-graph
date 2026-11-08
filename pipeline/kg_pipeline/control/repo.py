"""Queries and serializers for sources and runs. Serializers never return the secret."""
import os

from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from . import secrets
from .db import runs, sources


def _iso(value):
    return value.isoformat() if value else None


def dagster_url(dagster_run_id: str | None) -> str | None:
    """Link to the run in the Dagster UI. Null when DAGSTER_WEBSERVER_URL is unset."""
    base = os.environ.get("DAGSTER_WEBSERVER_URL", "").strip().rstrip("/")
    return f"{base}/runs/{dagster_run_id}" if base and dagster_run_id else None


def serialize_run(row) -> dict:
    return {
        "id": row.id, "source_id": row.source_id, "source_name": row.source_name, "kind": row.kind,
        "status": row.status, "trigger": row.trigger, "mode": row.mode, "dagster_run_id": row.dagster_run_id,
        "dagster_url": dagster_url(row.dagster_run_id), "stats": row.stats, "error": row.error,
        "started_at": _iso(row.started_at), "ended_at": _iso(row.ended_at), "requested_by": row.requested_by,
    }


def last_run(conn: Connection, source_id: str) -> dict | None:
    rows = conn.execute(select(runs).where(runs.c.source_id == source_id)).all()
    if not rows:
        return None
    # newest first: runs without started_at (queued) count as newest
    newest = max(rows, key=lambda r: (r.started_at is None, r.started_at.replace(tzinfo=None) if r.started_at else 0))
    return serialize_run(newest)


def serialize_source(conn: Connection, row) -> dict:
    keys: list[str] = []
    if row.secret:
        try:
            keys = secrets.secret_keys(secrets.decrypt(row.secret))
        except secrets.SecretKeyError:
            keys = []
    return {
        "id": row.id, "kind": row.kind, "name": row.name, "enabled": bool(row.enabled), "schedule": row.schedule,
        "config": row.config, "has_secret": bool(row.secret), "secret_keys": keys, "origin": row.origin,
        "last_run": last_run(conn, row.id), "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
    }


def list_sources(conn: Connection) -> list:
    return conn.execute(select(sources).order_by(sources.c.name)).all()


def get_source(conn: Connection, source_id: str):
    return conn.execute(select(sources).where(sources.c.id == source_id)).first()


def name_taken(conn: Connection, name: str) -> bool:
    return conn.execute(select(sources.c.id).where(sources.c.name == name)).first() is not None


def detach_runs(conn: Connection, source_id: str) -> None:
    """Set runs.source_id to null. PostgreSQL does this through ON DELETE SET NULL; the explicit
    update keeps SQLite (foreign keys off by default) identical."""
    conn.execute(update(runs).where(runs.c.source_id == source_id).values(source_id=None))
