import json
import os
import re
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import insert, select

from kg_pipeline.control import db, secrets, validate
from kg_pipeline.control.validate import ValidationError

GOOD = {
    "git": {"url": "https://github.com/acme/repo.git"},
    "jira": {"base_url": "https://acme.atlassian.net", "projects": ["A", "B"]},
    "confluence": {"base_url": "https://acme.atlassian.net/wiki", "spaces": ["ENG"]},
    "s3": {"bucket": "docs", "prefix": "kb/", "region": "eu-west-1", "include": ["*.md"]},
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("KG_CONTROL_ALLOW_HTTP", raising=False)
    monkeypatch.setenv("KG_SECRET_KEY", Fernet.generate_key().decode())


@pytest.mark.parametrize("kind", list(GOOD))
def test_valid_config_per_kind(kind):
    out = validate.validate_config(kind, GOOD[kind])
    for k, v in GOOD[kind].items():
        assert out[k] == v


def test_git_branch_default():
    assert validate.validate_config("git", GOOD["git"])["branch"] == "main"


@pytest.mark.parametrize("kind", list(GOOD))
def test_unknown_config_key_rejected(kind):
    with pytest.raises(ValidationError, match="unknown config key"):
        validate.validate_config(kind, {**GOOD[kind], "bogus": 1})


@pytest.mark.parametrize("name", ["", "Bad", "-x", "a_b", "a" * 64, None, "a b"])
def test_bad_name(name):
    with pytest.raises(ValidationError):
        validate.validate_name(name)


def test_good_name():
    assert validate.validate_name("repo-1") == "repo-1"
    assert validate.validate_name("a" * 63)


@pytest.mark.parametrize("kind,field", [("git", "url"), ("jira", "base_url"), ("confluence", "base_url")])
def test_http_rejected_unless_allowed(kind, field, monkeypatch):
    cfg = {**GOOD[kind], field: "http://example.com/x"}
    with pytest.raises(ValidationError, match="https"):
        validate.validate_config(kind, cfg)
    monkeypatch.setenv("KG_CONTROL_ALLOW_HTTP", "1")
    assert validate.validate_config(kind, cfg)[field] == "http://example.com/x"


def test_required_fields_and_url_credentials():
    with pytest.raises(ValidationError):
        validate.validate_config("git", {})
    with pytest.raises(ValidationError):
        validate.validate_config("jira", {"base_url": "https://a.example", "projects": []})
    with pytest.raises(ValidationError, match="credentials"):
        validate.validate_config("git", {"url": "https://user:pw@example.com/r.git"})
    with pytest.raises(ValidationError):
        validate.validate_config("s3", {})
    with pytest.raises(ValidationError):
        validate.validate_config("nope", {})
    with pytest.raises(ValidationError):
        validate.validate_config("git", "text")


def test_secret_keys_per_kind():
    assert validate.validate_secret("jira", {"email": "a@b.c", "api_token": "t"})
    with pytest.raises(ValidationError, match="unknown secret key"):
        validate.validate_secret("git", {"api_token": "t"})
    with pytest.raises(ValidationError) as e:
        validate.validate_secret("git", {"token": 5})
    assert "5" not in str(e.value)


def test_schedule():
    assert validate.validate_schedule(None) is None
    assert validate.validate_schedule("*/5 * * * *") == "*/5 * * * *"
    for bad in ("bad", "* * * *", "61 * * * *", 5):
        with pytest.raises(ValidationError):
            validate.validate_schedule(bad)


def test_fernet_round_trip_and_token_not_plain():
    secret = {"email": "a@b.c", "api_token": "s3cr3t-value"}
    token = secrets.encrypt(secret)
    assert "s3cr3t-value" not in token
    assert secrets.decrypt(token) == secret
    assert secrets.secret_keys(secret) == ["api_token", "email"]
    assert secrets.secret_keys(None) == []


def test_stored_secret_column_is_token():
    eng = db.get_engine("sqlite://")
    db.create_all(eng)
    db.create_all(eng)  # idempotent
    now = datetime.now(timezone.utc)
    with eng.begin() as c:
        c.execute(insert(db.sources).values(
            id=db.new_id("src"), kind="git", name="r", config={}, secret=secrets.encrypt({"token": "tok-123"}),
            enabled=True, origin="ui", created_at=now, updated_at=now))
        raw = c.execute(select(db.sources.c.secret)).scalar_one()
    assert "tok-123" not in raw
    assert json.loads(Fernet(os.environ["KG_SECRET_KEY"].encode()).decrypt(raw.encode())) == {"token": "tok-123"}


def test_new_id_format():
    assert re.fullmatch(r"src_[0-9a-f]{12}", db.new_id("src"))
    assert re.fullmatch(r"run_[0-9a-f]{12}", db.new_id("run"))


def test_missing_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("KG_SECRET_KEY")
    with pytest.raises(secrets.SecretKeyError, match="KG_SECRET_KEY"):
        secrets.encrypt({"token": "x"})
    with pytest.raises(secrets.SecretKeyError, match="KG_SECRET_KEY"):
        secrets.check_key()
    monkeypatch.setenv("KG_SECRET_KEY", "not-a-key")
    with pytest.raises(secrets.SecretKeyError, match="KG_SECRET_KEY"):
        secrets.check_key()


def test_get_engine_requires_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        db.get_engine()
