"""Scheduled launches of registry sources (sensor `due_sources` in definitions.py) and the run-status copy.

Cron check: Dagster's own cron iterator (`dagster._utils.schedules`, private API of the pinned dagster==1.13.28)
instead of a new `croniter` dependency. It is already installed with the `dagster` extra, it is the code that
validates the expression in the control API, and one function (`cron_is_due`) is the only place that uses it.

This module imports no fastapi. It reads the database when called, never at import.
"""
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone

import dagster as dg
from dagster._utils.schedules import get_next_cron_tick, is_valid_cron_string
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from .control import db, probe
from .control.dagster_client import ACTIVE, JOB_BY_KIND, launch_tags, map_status  # noqa: F401  (JOB_BY_KIND re-exported)

log = logging.getLogger(__name__)

FINAL = ("succeeded", "failed", "canceled")
QUEUE_TIMEOUT_S = 600  # a queued row without a Dagster run after this many seconds failed to launch
LAUNCH_LOST = "launch did not reach Dagster"


def _queue_timeout() -> int:
    try:
        return int(os.environ.get("KG_CONTROL_QUEUE_TIMEOUT_S", QUEUE_TIMEOUT_S))
    except ValueError:
        return QUEUE_TIMEOUT_S


def _expired(row, now: datetime) -> bool:
    """True for a queued row with no Dagster run that is older than the timeout. No `created_at` counts as not expired."""
    if row.status != "queued" or row.dagster_run_id or row.created_at is None:
        return False
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds() > _queue_timeout()


def cron_is_due(cron: str, when: datetime) -> bool:
    """True when the 5-field cron expression has a tick in the minute that holds `when` (evaluated in UTC)."""
    if not is_valid_cron_string(cron) or len(cron.split()) != 5:
        return False
    minute = when.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return get_next_cron_tick(cron, minute - timedelta(seconds=1), "UTC") == minute


def _run_id(source_id: str, minute: str) -> str:
    """Same id for the same source and minute: a second insert in that minute hits the primary key and is skipped."""
    return "run_" + hashlib.sha1(f"{source_id}:{minute}".encode()).hexdigest()[:12]


def due_requests(now: datetime) -> list[dg.RunRequest]:
    """Insert a queued `runs` row and build one RunRequest for each enabled source that is due and has no active run."""
    minute = now.astimezone(timezone.utc).strftime("%Y%m%d%H%M")
    requests: list[dg.RunRequest] = []
    engine = db.get_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(db.sources).where(
                db.sources.c.enabled.is_(True), db.sources.c.schedule.is_not(None),
                db.sources.c.kind.in_(list(JOB_BY_KIND))).order_by(db.sources.c.name)).all()
        for src in rows:
            if not cron_is_due(src.schedule, now):
                continue
            try:
                probe.guard_source(src.kind, src.config)
            except probe.Blocked as e:
                log.warning("source %s skipped: %s (set KG_CONTROL_ALLOW_PRIVATE=1 or KG_CONTROL_ALLOW_HTTP=1 on the "
                            "Dagster user code to allow an internal or http host)", src.name, e)
                continue
            run_id = _run_id(src.id, minute)
            try:
                # One transaction for each source: a lost race with a manual run (unique index on active runs)
                # skips this source only.
                with engine.begin() as conn:
                    if conn.execute(select(db.runs.c.id).where(
                            db.runs.c.source_id == src.id, db.runs.c.status.in_(ACTIVE))).first():
                        continue
                    if conn.execute(select(db.runs.c.id).where(db.runs.c.id == run_id)).first():
                        continue  # already launched in this minute (and finished)
                    conn.execute(insert(db.runs).values(
                        id=run_id, source_id=src.id, source_name=src.name, kind=src.kind, status="queued",
                        trigger="schedule", mode="incremental", requested_by="schedule", created_at=now))
            except IntegrityError:
                log.info("source %s skipped: a run became active first", src.name)
                continue
            tags = launch_tags(src.kind, src.name, src.id, run_id, "incremental")
            requests.append(dg.RunRequest(
                run_key=f"{src.id}:{minute}", job_name=JOB_BY_KIND[src.kind],
                partition_key=src.name if src.kind == "git" else None, tags=tags))
    finally:
        engine.dispose()
    return requests


def _json(value):
    value = getattr(value, "value", value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return str(value)


def _stats(instance, dagster_run_id: str) -> dict | None:
    try:
        events = instance.all_logs(dagster_run_id, of_type=dg.DagsterEventType.ASSET_MATERIALIZATION)
        out = {}
        for e in events:
            m = e.dagster_event.event_specific_data.materialization
            out["/".join(m.asset_key.path)] = {k: _json(v) for k, v in m.metadata.items()}
        return out or None
    except Exception:
        return None


def sync_runs(instance, now: datetime | None = None) -> int:
    """Copy the Dagster status of active `runs` rows (found by tag kg/run_id) into the table. Returns rows changed.

    A queued row that has no Dagster run after KG_CONTROL_QUEUE_TIMEOUT_S (default 600) is marked failed.
    This also stores `dagster_run_id` for a run that the sensor requested (the sensor cannot know it earlier).
    """
    changed = 0
    now = now or datetime.now(timezone.utc)
    engine = db.get_engine()
    try:
        with engine.connect() as conn:
            active = conn.execute(select(db.runs).where(db.runs.c.status.in_(ACTIVE))).all()
        for row in active:
            found = instance.get_runs(filters=dg.RunsFilter(tags={"kg/run_id": row.id}), limit=1)
            if not found:
                if _expired(row, now):
                    with engine.begin() as conn:
                        conn.execute(update(db.runs).where(db.runs.c.id == row.id, db.runs.c.status == "queued",
                                                           db.runs.c.dagster_run_id.is_(None))
                                     .values(status="failed", error=LAUNCH_LOST, ended_at=now))
                    changed += 1
                continue
            status = map_status(found[0].status.value)
            values: dict = {}
            if row.dagster_run_id != found[0].run_id:
                values["dagster_run_id"] = found[0].run_id
            if status != row.status:
                values["status"] = status
                if status != "queued" and row.started_at is None:
                    values["started_at"] = now
                if status in FINAL:
                    values["ended_at"] = now
                    values["stats"] = _stats(instance, found[0].run_id)
            if values:
                with engine.begin() as conn:
                    conn.execute(update(db.runs).where(db.runs.c.id == row.id, db.runs.c.status.in_(ACTIVE))
                                 .values(**values))
                changed += 1
    finally:
        engine.dispose()
    return changed
