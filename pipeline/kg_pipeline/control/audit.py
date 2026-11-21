"""Audit writes. `detail` holds names and flags only, never a secret value."""
from datetime import datetime, timezone

from sqlalchemy import insert
from sqlalchemy.engine import Connection

from .db import audit, new_id


def write(conn: Connection, actor: str, action: str, target: str | None, detail: dict | None = None) -> None:
    conn.execute(insert(audit).values(
        id=new_id("aud"), at=datetime.now(timezone.utc), actor=actor, action=action, target=target, detail=detail))
