"""Sources CRUD. Secrets are write-only: stored as a Fernet token, never returned."""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from sqlalchemy import delete as sql_delete, insert, update
from sqlalchemy.engine import Engine

from . import audit, dagster_client, probe, repo, secrets, validate
from .auth import User, require_admin
from .db import new_id, sources
from .routes_runs import get_dagster

log = logging.getLogger(__name__)


def _engine(request: Request) -> Engine:
    return request.app.state.engine


def _bad(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


def _enabled(value) -> bool:
    if not isinstance(value, bool):
        raise validate.ValidationError("enabled must be true or false")
    return value


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/sources")
    def list_(user: User = Depends(require_admin), engine: Engine = Depends(_engine)):
        with engine.connect() as conn:
            return {"items": [repo.serialize_source(conn, r) for r in repo.list_sources(conn)]}

    @router.post("/sources", status_code=201)
    def create(request: Request, body: dict = Body(...), user: User = Depends(require_admin),
               engine: Engine = Depends(_engine)):
        try:
            unknown = sorted(set(body) - {"kind", "name", "config", "secret", "schedule", "enabled"})
            if unknown:
                raise validate.ValidationError(f"unknown field(s): {', '.join(unknown)}")
            kind = validate.validate_kind(body.get("kind"))
            name = validate.validate_name(body.get("name"))
            config = validate.validate_config(kind, body.get("config"))
            schedule = validate.validate_schedule(body.get("schedule"))
            enabled = _enabled(body["enabled"]) if "enabled" in body else True
            token = None
            secret = body.get("secret")
            if secret:
                token = secrets.encrypt(validate.validate_secret(kind, secret))
            elif secret is not None and not isinstance(secret, dict):
                raise validate.ValidationError("secret must be an object")
        except (validate.ValidationError, secrets.SecretKeyError) as e:
            raise _bad(e) from e
        now = datetime.now(timezone.utc)
        source_id = new_id("src")
        with engine.begin() as conn:
            if repo.name_taken(conn, name):
                raise HTTPException(409, f"a source named {name} already exists")
            conn.execute(insert(sources).values(
                id=source_id, kind=kind, name=name, config=config, secret=token, schedule=schedule, enabled=enabled,
                origin="ui", created_at=now, updated_at=now, created_by=user.name))
            if kind == "git":
                # A git run needs its partition. If Dagster fails, leaving this block rolls the row back.
                try:
                    get_dagster(request).add_partition(name)
                except dagster_client.DagsterError as e:
                    log.warning("source %s: add_partition failed: %s", name, e)
                    raise HTTPException(502, "dagster could not register the repository; the source was not created") from e
            audit.write(conn, user.name, "source.create", source_id,
                        {"name": name, "kind": kind, "secret_keys": secrets.secret_keys(secret if token else None)})
            return repo.serialize_source(conn, repo.get_source(conn, source_id))

    @router.get("/sources/{source_id}")
    def get(source_id: str, user: User = Depends(require_admin), engine: Engine = Depends(_engine)):
        with engine.connect() as conn:
            row = repo.get_source(conn, source_id)
            if row is None:
                raise HTTPException(404, "source not found")
            return repo.serialize_source(conn, row)

    @router.patch("/sources/{source_id}")
    def patch(source_id: str, body: dict = Body(...), user: User = Depends(require_admin),
              engine: Engine = Depends(_engine)):
        with engine.begin() as conn:
            row = repo.get_source(conn, source_id)
            if row is None:
                raise HTTPException(404, "source not found")
            values: dict = {}
            changed: dict = {}
            try:
                unknown = sorted(set(body) - {"config", "secret", "schedule", "enabled"})
                if unknown:
                    raise validate.ValidationError(f"unknown field(s): {', '.join(unknown)}")
                if "config" in body:
                    values["config"] = validate.validate_config(row.kind, body["config"])
                    changed["config"] = True
                if "schedule" in body:
                    values["schedule"] = validate.validate_schedule(body["schedule"])
                    changed["schedule"] = values["schedule"]
                if "enabled" in body:
                    values["enabled"] = _enabled(body["enabled"])
                    changed["enabled"] = values["enabled"]
                if body.get("secret") is not None:
                    if not isinstance(body["secret"], dict):
                        raise validate.ValidationError("secret must be an object")
                    if body["secret"]:
                        values["secret"] = secrets.encrypt(validate.validate_secret(row.kind, body["secret"]))
                        changed["secret"] = "replaced"
                        changed["secret_keys"] = secrets.secret_keys(body["secret"])
                    else:
                        values["secret"] = None
                        changed["secret"] = "cleared"
            except (validate.ValidationError, secrets.SecretKeyError) as e:
                raise _bad(e) from e
            values["updated_at"] = datetime.now(timezone.utc)
            conn.execute(update(sources).where(sources.c.id == source_id).values(**values))
            audit.write(conn, user.name, "source.update", source_id, {"name": row.name, "changed": changed})
            return repo.serialize_source(conn, repo.get_source(conn, source_id))

    @router.delete("/sources/{source_id}", status_code=204)
    def delete(source_id: str, request: Request, user: User = Depends(require_admin),
               engine: Engine = Depends(_engine)):
        with engine.begin() as conn:
            row = repo.get_source(conn, source_id)
            if row is None:
                raise HTTPException(404, "source not found")
            repo.detach_runs(conn, source_id)
            conn.execute(sql_delete(sources).where(sources.c.id == source_id))
            detail = {"name": row.name, "kind": row.kind}
            if row.kind == "git":
                # The source is gone either way; a Dagster failure is logged and reported in the audit detail.
                try:
                    get_dagster(request).remove_partition(row.name)
                    detail["partition_removed"] = True
                except dagster_client.DagsterError as e:
                    log.warning("source %s: remove_partition failed: %s", row.name, e)
                    detail["partition_removed"] = False
            audit.write(conn, user.name, "source.delete", source_id, detail)
        return Response(status_code=204)

    @router.post("/sources/{source_id}/test")
    def test(source_id: str, request: Request, user: User = Depends(require_admin),
             engine: Engine = Depends(_engine)):
        with engine.connect() as conn:
            row = repo.get_source(conn, source_id)
        if row is None:
            raise HTTPException(404, "source not found")
        try:
            secret = secrets.decrypt(row.secret) if row.secret else None
            prober = getattr(request.app.state, "prober", None) or probe.Prober()
            result = prober.test(row.kind, row.config, secret)
        except secrets.SecretKeyError as e:
            result = {"ok": False, "message": str(e), "details": {}}
        with engine.begin() as conn:
            audit.write(conn, user.name, "source.test", source_id, {"kind": row.kind, "ok": result["ok"]})
        return result

    return router
