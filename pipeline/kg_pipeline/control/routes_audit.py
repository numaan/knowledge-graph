"""Audit route: `GET /audit?limit=` lists audit entries, newest first."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.engine import Engine
from fastapi import Request

from .auth import User, require_admin
from .db import audit

DEFAULT_LIMIT, MAX_LIMIT = 50, 200


def _engine(request: Request) -> Engine:
    return request.app.state.engine


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/audit")
    def list_audit(limit: str | None = None, user: User = Depends(require_admin),
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
        with engine.connect() as conn:
            rows = conn.execute(select(audit).order_by(audit.c.at.desc(), audit.c.id.desc()).limit(n)).all()
        return {"items": [{"id": r.id, "at": r.at.isoformat() if r.at else None, "actor": r.actor,
                           "action": r.action, "target": r.target, "detail": r.detail} for r in rows]}

    return router
