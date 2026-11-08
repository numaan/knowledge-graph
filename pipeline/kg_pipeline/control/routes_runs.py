"""Run routes. `POST /sources/{id}/run` launches the Dagster job of the source kind."""
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from . import audit, dagster_client, probe, repo, secrets
from .auth import User, require_admin
from .db import new_id, runs


def _engine(request: Request) -> Engine:
    return request.app.state.engine


def get_dagster(request: Request) -> dagster_client.DagsterClient:
    """Tests set `app.state.dagster`; production builds the client from env on first use."""
    client = getattr(request.app.state, "dagster", None)
    if client is None:
        client = request.app.state.dagster = dagster_client.DagsterClient.from_env()
    return client


FINAL = ("succeeded", "failed", "canceled")
DEFAULT_LIMIT, MAX_LIMIT, LOG_LINES = 50, 200, 200


def _refresh(engine: Engine, client, row):
    """Pull the status from Dagster into `runs`. Return the fresh row. A Dagster error keeps the stored row."""
    if row.status not in dagster_client.ACTIVE or not row.dagster_run_id:
        return row
    try:
        new = dagster_client.map_status(client.get_status(row.dagster_run_id))
    except dagster_client.DagsterError:
        return row
    if new == row.status:
        return row
    now = datetime.now(timezone.utc)
    values: dict = {"status": new}
    if new != "queued" and row.started_at is None:
        values["started_at"] = now
    if new in FINAL:
        values["ended_at"] = now
        # Asset metadata: a client that can read it fills `stats` (None when unavailable).
        reader = getattr(client, "get_stats", None)
        if reader:
            try:
                values["stats"] = reader(row.dagster_run_id)
            except dagster_client.DagsterError:
                pass
    with engine.begin() as conn:
        # guard: do not overwrite a run that another request already moved to a final status
        conn.execute(update(runs).where(runs.c.id == row.id, runs.c.status.in_(dagster_client.ACTIVE)).values(**values))
        return conn.execute(select(runs).where(runs.c.id == row.id)).one()


def _scrub_text(text: str, token: str | None) -> str:
    """Mask the stored secret values in a Dagster message. Without a readable secret, keep no detail."""
    if not token:
        return text
    try:
        return probe._scrub({"t": text}, secrets.decrypt(token))["t"]
    except secrets.SecretKeyError:
        return "dagster reported an error"


def _stored_secret(engine: Engine, source_id: str | None) -> str | None:
    if not source_id:
        return None
    with engine.connect() as conn:
        src = repo.get_source(conn, source_id)
    return src.secret if src is not None else None


def _log_tail(engine: Engine, client, row) -> list[str]:
    if not row.dagster_run_id:
        return []
    try:
        lines = [str(x) for x in client.get_logs(row.dagster_run_id)][-LOG_LINES:]
    except dagster_client.DagsterError:
        return []
    secret: dict = {}
    if row.source_id:
        with engine.connect() as conn:
            src = repo.get_source(conn, row.source_id)
        if src is not None and src.secret:
            try:
                secret = secrets.decrypt(src.secret)
            except secrets.SecretKeyError:
                return []  # cannot scrub, so show no log text
    return probe._scrub({"lines": lines}, secret)["lines"] if secret else lines


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/runs")
    def list_runs(request: Request, source_id: str | None = None, status: str | None = None,
                  limit: str | None = None, user: User = Depends(require_admin),
                  engine: Engine = Depends(_engine)):
        n = DEFAULT_LIMIT
        if limit is not None:
            try:
                n = int(limit)
            except ValueError:
                raise HTTPException(400, "limit must be an integer")
            if n < 1:
                raise HTTPException(400, "limit must be 1 or more")
            n = min(n, MAX_LIMIT)
        q = select(runs)
        if source_id:
            q = q.where(runs.c.source_id == source_id)
        if status:
            q = q.where(runs.c.status == status)
        # a run without started_at (queued) counts as newest
        q = q.order_by(runs.c.started_at.is_(None).desc(), runs.c.started_at.desc(), runs.c.id.desc()).limit(n)
        with engine.connect() as conn:
            rows = conn.execute(q).all()
        if any(r.status in dagster_client.ACTIVE and r.dagster_run_id for r in rows):
            client = get_dagster(request)
            rows = [_refresh(engine, client, r) for r in rows]
        return {"items": [repo.serialize_run(r) for r in rows]}

    @router.get("/runs/{run_id}")
    def get_run(run_id: str, request: Request, user: User = Depends(require_admin),
                engine: Engine = Depends(_engine)):
        with engine.connect() as conn:
            row = conn.execute(select(runs).where(runs.c.id == run_id)).first()
        if row is None:
            raise HTTPException(404, "run not found")
        client = get_dagster(request)
        row = _refresh(engine, client, row)
        return {**repo.serialize_run(row), "log_tail": _log_tail(engine, client, row)}

    @router.post("/runs/{run_id}/cancel")
    def cancel(run_id: str, request: Request, user: User = Depends(require_admin),
               engine: Engine = Depends(_engine)):
        with engine.connect() as conn:
            row = conn.execute(select(runs).where(runs.c.id == run_id)).first()
        if row is None:
            raise HTTPException(404, "run not found")
        if row.status in FINAL:
            raise HTTPException(409, f"run is already {row.status}")
        if row.dagster_run_id:
            try:
                get_dagster(request).terminate(row.dagster_run_id)
            except dagster_client.DagsterUnavailable:
                raise HTTPException(502, "dagster is unreachable")
            except dagster_client.DagsterError as e:
                raise HTTPException(502, _scrub_text(str(e), _stored_secret(engine, row.source_id))[:200])
        with engine.begin() as conn:
            conn.execute(update(runs).where(runs.c.id == run_id).values(
                status="canceled", ended_at=datetime.now(timezone.utc)))
            audit.write(conn, user.name, "run.cancel", run_id, {"source": row.source_name, "kind": row.kind})
            return repo.serialize_run(conn.execute(select(runs).where(runs.c.id == run_id)).one())

    @router.post("/sources/{source_id}/run", status_code=202)
    def start(source_id: str, request: Request, body: dict | None = Body(None), user: User = Depends(require_admin),
              engine: Engine = Depends(_engine)):
        body = body or {}
        if set(body) - {"mode"}:
            raise HTTPException(400, f"unknown field(s): {', '.join(sorted(set(body) - {'mode'}))}")
        mode = body.get("mode", "incremental")
        if mode not in ("incremental", "full"):
            raise HTTPException(400, "mode must be incremental or full")
        with engine.begin() as conn:
            src = repo.get_source(conn, source_id)
            if src is None:
                raise HTTPException(404, "source not found")
            if src.kind not in dagster_client.JOB_BY_KIND:
                raise HTTPException(501, "s3_documents asset not available yet")
            if not src.enabled:
                raise HTTPException(409, "source is disabled")
            active = conn.execute(select(runs.c.id).where(
                runs.c.source_id == source_id, runs.c.status.in_(dagster_client.ACTIVE))).first()
            if active:
                raise HTTPException(409, f"source already has an active run ({active.id})")
            run_id = new_id("run")
            conn.execute(insert(runs).values(
                id=run_id, source_id=source_id, source_name=src.name, kind=src.kind, status="queued",
                trigger="manual", mode=mode, requested_by=user.name))
        tags = dagster_client.launch_tags(src.kind, src.name, source_id, run_id, mode)
        error = None
        try:
            dagster_run_id = get_dagster(request).launch(dagster_client.JOB_BY_KIND[src.kind], tags)
        except dagster_client.DagsterUnavailable:
            error = "dagster is unreachable"
        except dagster_client.DagsterError as e:
            error = _scrub_text(str(e), src.secret)[:200]
        with engine.begin() as conn:
            if error:
                conn.execute(update(runs).where(runs.c.id == run_id).values(
                    status="failed", error=error, ended_at=datetime.now(timezone.utc)))
            else:
                conn.execute(update(runs).where(runs.c.id == run_id).values(dagster_run_id=dagster_run_id))
                audit.write(conn, user.name, "run.start", run_id,
                            {"source": src.name, "kind": src.kind, "mode": mode, "dagster_run_id": dagster_run_id})
            row = conn.execute(select(runs).where(runs.c.id == run_id)).one()
            result = repo.serialize_run(row)
        if error:
            raise HTTPException(502, error)
        return result

    return router
