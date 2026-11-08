"""Run routes. `POST /sources/{id}/run` launches the Dagster job of the source kind."""
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from . import audit, dagster_client, repo
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


def make_router() -> APIRouter:
    router = APIRouter()

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
        tags = {"kg/source": src.name, "kg/source_id": source_id, "kg/run_id": run_id, "kg/mode": mode}
        if src.kind == "git":
            tags[dagster_client.PARTITION_TAG] = src.name
        error = None
        try:
            dagster_run_id = get_dagster(request).launch(dagster_client.JOB_BY_KIND[src.kind], tags)
        except dagster_client.DagsterUnavailable:
            error = "dagster is unreachable"
        except dagster_client.DagsterError as e:
            error = str(e)[:200]
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
