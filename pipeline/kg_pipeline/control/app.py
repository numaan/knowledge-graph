"""FastAPI app factory for the control API. Errors are `{"error": "text"}`."""
import os

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from sqlalchemy.engine import Engine

from . import db, routes_runs, routes_sources, secrets
from .auth import Authenticator, User, require_user


def check_startup() -> None:
    """Exit with a clear error when DATABASE_URL is set and KG_SECRET_KEY is missing or invalid."""
    if os.environ.get("DATABASE_URL", "").strip():
        try:
            secrets.check_key()
        except secrets.SecretKeyError as e:
            raise SystemExit(f"control API cannot start: {e}") from e


def create_app(auth: Authenticator | None = None, engine: Engine | None = None) -> FastAPI:
    """With no engine, DATABASE_URL is used when set; with neither, only /healthz and /me are served."""
    check_startup()
    app = FastAPI(title="kg control API", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth or Authenticator.from_env()
    if engine is None and os.environ.get("DATABASE_URL", "").strip():
        engine = db.get_engine()
    app.state.engine = engine
    if engine is not None:
        db.create_all(engine)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        return JSONResponse({"error": f"invalid request: {loc} {first.get('msg', '')}".strip()}, status_code=400)

    api = APIRouter(prefix="/api/v1")

    @api.get("/healthz")
    def healthz():
        return {"ok": True}

    @api.get("/me")
    def me(user: User = Depends(require_user)):
        return {"user": user.name, "is_admin": user.is_admin}

    if engine is not None:
        api.include_router(routes_sources.make_router())
        api.include_router(routes_runs.make_router())

    app.include_router(api)
    return app
