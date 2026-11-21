"""Sources registry: env fallback, seed from env, secrets, no database at import."""
import logging
import subprocess
import sys
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select, update
from sqlalchemy.pool import StaticPool

from kg_pipeline import sources_registry as reg
from kg_pipeline.control import db

ENV = {
    "GIT_REPOSITORIES": "https://git.example.com/org/repo-a.git,https://git.example.com/org/repo-b",
    "GIT_BRANCH": "dev", "GIT_TOKEN": "gtok",
    "JIRA_PROJECTS": "AB, CD", "JIRA_BASE_URL": "https://x.atlassian.net", "JIRA_EMAIL": "j@x.io",
    "JIRA_API_TOKEN": "jtok",
    "CONFLUENCE_SPACES": "ENG", "CONFLUENCE_BASE_URL": "https://x.atlassian.net/wiki",
    "CONFLUENCE_EMAIL": "c@x.io", "CONFLUENCE_API_TOKEN": "ctok",
}


@pytest.fixture
def env(monkeypatch):
    for k in ("DATABASE_URL", "KG_SECRET_KEY", "ATLASSIAN_EMAIL", "ATLASSIAN_API_TOKEN", *ENV):
        monkeypatch.delenv(k, raising=False)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    return monkeypatch


@pytest.fixture
def engine():
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    db.create_all(e)
    return e


def rows(engine):
    with engine.connect() as c:
        return {r["name"]: r for r in c.execute(select(db.sources)).mappings()}


def test_env_fallback_without_database_url(env):
    git = reg.get_sources("git")
    assert [(s.name, s.config) for s in git] == [
        ("repo-a", {"url": "https://git.example.com/org/repo-a.git", "branch": "dev"}),
        ("repo-b", {"url": "https://git.example.com/org/repo-b", "branch": "dev"})]
    assert reg.get_secret(git[0]) == {"token": "gtok"}
    jira = reg.get_sources("jira")
    assert jira[0].config == {"base_url": "https://x.atlassian.net", "projects": ["AB", "CD"]}
    assert reg.get_secret(jira[0]) == {"email": "j@x.io", "api_token": "jtok"}
    conf = reg.get_sources("confluence")
    assert conf[0].config["spaces"] == ["ENG"]
    assert reg.get_sources("s3") == []


def test_env_empty(env):
    env.delenv("JIRA_PROJECTS")
    assert reg.get_sources("jira") == []


def test_seed_twice_is_idempotent(env, engine):
    env.setenv("KG_SECRET_KEY", Fernet.generate_key().decode())
    assert reg.seed_from_env(engine) == 4
    assert reg.seed_from_env(engine) == 0
    r = rows(engine)
    assert sorted(r) == ["confluence", "jira", "repo-a", "repo-b"]
    assert {v["origin"] for v in r.values()} == {"env"}
    assert r["repo-a"]["secret"] and "gtok" not in r["repo-a"]["secret"]


def test_seed_without_key_stores_no_secret(env, engine):
    reg.seed_from_env(engine)
    assert all(v["secret"] is None for v in rows(engine).values())


def test_edited_env_row_not_overwritten(env, engine):
    reg.seed_from_env(engine)
    with engine.begin() as c:
        c.execute(update(db.sources).where(db.sources.c.name == "repo-a")
                  .values(config={"url": "https://other/x.git", "branch": "z"},
                          updated_at=rows(engine)["repo-a"]["created_at"] + timedelta(seconds=5)))
    env.setenv("GIT_BRANCH", "other")
    reg.seed_from_env(engine)
    r = rows(engine)
    assert r["repo-a"]["config"] == {"url": "https://other/x.git", "branch": "z"}
    assert r["repo-b"]["config"]["branch"] == "other"  # unedited env row follows the env


def test_invalid_name_skipped_with_warning(env, engine, caplog):
    env.setenv("GIT_REPOSITORIES", "https://git.example.com/org/Bad_Name.git,https://git.example.com/o/ok.git")
    with caplog.at_level(logging.WARNING):
        reg.seed_from_env(engine)
    assert "Bad_Name" in caplog.text
    assert "ok" in rows(engine) and "Bad_Name" not in rows(engine)


def test_get_sources_reads_database_at_call_time(env, engine, monkeypatch):
    key = Fernet.generate_key().decode()
    env.setenv("KG_SECRET_KEY", key)
    reg.seed_from_env(engine)
    env.setenv("DATABASE_URL", "sqlite://")
    env.setattr(db, "get_engine", lambda url=None: engine)
    env.setattr(engine, "dispose", lambda: None)
    env.delenv("GIT_REPOSITORIES")  # the database, not the env, is the source now
    git = reg.get_sources("git")
    assert [s.name for s in git] == ["repo-a", "repo-b"]
    assert reg.get_secret(git[0]) == {"token": "gtok"}
    with engine.begin() as c:
        c.execute(update(db.sources).where(db.sources.c.name == "repo-b").values(enabled=False))
    assert [s.name for s in reg.get_sources("git")] == ["repo-a"]


def test_row_without_secret_gives_empty_secret(env, engine):
    reg.seed_from_env(engine)
    env.setenv("DATABASE_URL", "sqlite://")
    env.setattr(db, "get_engine", lambda url=None: engine)
    env.setattr(engine, "dispose", lambda: None)
    assert reg.get_secret(reg.get_sources("git")[0]) == {}


def test_import_opens_no_connection():
    code = (
        "import sqlalchemy\n"
        "def boom(*a, **k): raise AssertionError('engine created')\n"
        "sqlalchemy.create_engine = boom\n"
        "import kg_pipeline.sources_registry, kg_pipeline.definitions\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**__import__("os").environ, "DATABASE_URL": "postgresql://x/y"})
    assert r.returncode == 0, r.stderr


def test_create_app_seeds_when_database_url_set(env, engine):
    from kg_pipeline.control.app import create_app
    from kg_pipeline.control.auth import Authenticator

    env.setenv("DATABASE_URL", "sqlite://")
    env.setenv("KG_SECRET_KEY", Fernet.generate_key().decode())
    create_app(auth=Authenticator.from_env(), engine=engine)
    assert "repo-a" in rows(engine)
