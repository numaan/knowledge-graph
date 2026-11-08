"""Source registry: enabled sources read at CALL time (never at import) from the control database.

With `DATABASE_URL` unset, `get_sources` returns the sources derived from the environment as before:
`GIT_REPOSITORIES`/`GIT_BRANCH`/`GIT_TOKEN`, `JIRA_PROJECTS` with `JIRA_BASE_URL`/`JIRA_EMAIL`/`JIRA_API_TOKEN`,
`CONFLUENCE_SPACES` with `CONFLUENCE_BASE_URL`/`CONFLUENCE_EMAIL`/`CONFLUENCE_API_TOKEN`.

`seed_from_env` copies those env sources into the database as rows with `origin = 'env'`. Env credentials are stored
as the encrypted secret only when `KG_SECRET_KEY` is set. Without the key the row has no secret, `get_secret` returns
`{}` and the asset falls back to the env credentials (`GIT_TOKEN`, `JIRA_EMAIL`, ...). That fallback is only for rows
with `origin = 'env'`. A UI row (`origin = 'ui'`) without a secret runs without credentials.
"""
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .sources import git

log = logging.getLogger(__name__)

JIRA_NAME = "jira"
CONFLUENCE_NAME = "confluence"


@dataclass
class Source:
    name: str
    kind: str
    config: dict
    schedule: str | None = None
    origin: str = "env"
    id: str | None = None
    encrypted_secret: str | None = field(default=None, repr=False)
    env_secret: dict = field(default_factory=dict, repr=False)


def _db_url() -> str:
    return os.environ.get("DATABASE_URL", "").strip()


def _csv(name: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]


def _env_secret(kind: str) -> dict:
    """Plain credentials from the environment for a kind. Empty keys are left out."""
    if kind == "git":
        pairs = {"token": os.environ.get("GIT_TOKEN", "")}
    else:
        prefix = kind.upper()
        pairs = {"email": os.environ.get(f"{prefix}_EMAIL") or os.environ.get("ATLASSIAN_EMAIL", ""),
                 "api_token": os.environ.get(f"{prefix}_API_TOKEN") or os.environ.get("ATLASSIAN_API_TOKEN", "")}
    return {k: v for k, v in pairs.items() if v}


def _env_sources(kind: str, with_secret: bool = True) -> list[Source]:
    """Sources derived from the environment. Names are not validated here."""
    secret = _env_secret(kind) if with_secret else {}
    if kind == "git":
        branch = os.environ.get("GIT_BRANCH", "main")
        urls = _csv("GIT_REPOSITORIES")
        return [Source(git.repo_name(u), kind, {"url": u, "branch": branch}, env_secret=secret) for u in urls]
    if kind == "jira" and _csv("JIRA_PROJECTS"):
        cfg = {"base_url": os.environ.get("JIRA_BASE_URL", ""), "projects": _csv("JIRA_PROJECTS")}
        return [Source(JIRA_NAME, kind, cfg, env_secret=secret)]
    if kind == "confluence" and _csv("CONFLUENCE_SPACES"):
        cfg = {"base_url": os.environ.get("CONFLUENCE_BASE_URL", ""), "spaces": _csv("CONFLUENCE_SPACES")}
        return [Source(CONFLUENCE_NAME, kind, cfg, env_secret=secret)]
    return []


def get_sources(kind: str) -> list[Source]:
    """Enabled sources of a kind. Reads the database when DATABASE_URL is set, else the environment."""
    if not _db_url():
        return _env_sources(kind)
    from sqlalchemy import select

    from .control import db

    engine = db.get_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(db.sources).where(db.sources.c.kind == kind, db.sources.c.enabled.is_(True))
                                .order_by(db.sources.c.name)).mappings().all()
    finally:
        engine.dispose()
    return [Source(r["name"], r["kind"], dict(r["config"]), r["schedule"], r["origin"], r["id"], r["secret"])
            for r in rows]


def get_secret(source: Source) -> dict:
    """Plain secret values of a source. `{}` when the source has no secret (only an env-origin source falls back to env)."""
    if source.encrypted_secret:
        from .control import secrets

        return secrets.decrypt(source.encrypted_secret)
    return dict(source.env_secret)


def _valid_name(name: str) -> bool:
    from .control import validate

    try:
        validate.validate_name(name)
    except validate.ValidationError:
        return False
    return True


def seed_from_env(engine=None) -> int:
    """Insert env sources as rows with origin='env'. Idempotent. Returns the number of rows inserted or updated.

    An existing env row that an admin edited (updated_at > created_at) is left alone, as is a row of another origin.
    """
    from sqlalchemy import insert, select, update

    from .control import db, secrets

    own = engine is None
    if own:
        engine = db.get_engine()
    try:
        db.create_all(engine)
        has_key = True
        try:
            secrets.check_key()
        except secrets.SecretKeyError:
            has_key = False
        changed = 0
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            for kind in ("git", "jira", "confluence"):
                for src in _env_sources(kind):
                    if not _valid_name(src.name):
                        log.warning("env seed: skipping %s source with invalid name %r", kind, src.name)
                        continue
                    if kind != "git" and not src.config["base_url"]:
                        log.warning("env seed: skipping %s source %r: base URL is not set", kind, src.name)
                        continue
                    token = secrets.encrypt(src.env_secret) if has_key and src.env_secret else None
                    row = conn.execute(select(db.sources).where(db.sources.c.name == src.name)).mappings().first()
                    if row is None:
                        conn.execute(insert(db.sources).values(
                            id=db.new_id("src"), kind=kind, name=src.name, config=src.config, secret=token,
                            schedule=None, enabled=True, origin="env", created_at=now, updated_at=now,
                            created_by="env"))
                        changed += 1
                    elif row["origin"] == "env" and row["updated_at"] == row["created_at"]:
                        same_secret = (secrets.decrypt(row["secret"]) if row["secret"] and has_key else None) == (
                            src.env_secret if token else None)
                        if row["config"] != src.config or (has_key and not same_secret):
                            values = {"config": src.config}
                            if has_key:
                                values["secret"] = token
                            conn.execute(update(db.sources).where(db.sources.c.id == row["id"]).values(**values))
                            changed += 1
        return changed
    finally:
        if own:
            engine.dispose()
