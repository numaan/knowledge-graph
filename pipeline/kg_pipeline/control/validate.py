"""Validation of source name, config, secret and schedule per kind (docs/streams/admin.md section 5)."""
import os
import re
from urllib.parse import urlparse

KINDS = ("git", "s3", "confluence", "jira")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

SECRET_KEYS = {
    "git": {"token"},
    "jira": {"email", "api_token"},
    "confluence": {"email", "api_token"},
    "s3": {"access_key_id", "secret_access_key"},
}

_PROJECT_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_~-]{0,63}$")


class ValidationError(ValueError):
    """The request body is not valid. The message is safe to return to the caller."""


def _allow_http() -> bool:
    return os.environ.get("KG_CONTROL_ALLOW_HTTP") == "1"


def validate_name(name) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValidationError("name must match [a-z0-9][a-z0-9-]{0,62}")
    return name


def validate_kind(kind) -> str:
    if kind not in KINDS:
        raise ValidationError(f"kind must be one of: {', '.join(KINDS)}")
    return kind


def _url(config: dict, field: str, required: bool = True) -> str | None:
    value = config.get(field)
    if value in (None, ""):
        if required:
            raise ValidationError(f"config.{field} is required")
        return None
    if not isinstance(value, str):
        raise ValidationError(f"config.{field} must be a string")
    parsed = urlparse(value)
    schemes = ("https", "http") if _allow_http() else ("https",)
    if parsed.scheme not in schemes or not parsed.hostname:
        raise ValidationError(f"config.{field} must be an https:// URL" + (" (or http://)" if _allow_http() else ""))
    if parsed.username or parsed.password:
        raise ValidationError(f"config.{field} must not contain credentials; use the secret")
    return value


def _str(config: dict, field: str, required: bool = False, default: str | None = None) -> str | None:
    value = config.get(field)
    if value in (None, ""):
        if required:
            raise ValidationError(f"config.{field} is required")
        return default
    if not isinstance(value, str):
        raise ValidationError(f"config.{field} must be a string")
    return value


def _str_list(config: dict, field: str, required: bool, pattern: re.Pattern | None = None) -> list[str]:
    value = config.get(field)
    if value in (None, []):
        if required:
            raise ValidationError(f"config.{field} must be a non-empty list")
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValidationError(f"config.{field} must be a list of strings")
    if pattern and not all(pattern.match(v) for v in value):
        raise ValidationError(f"config.{field} has an invalid entry")
    return value


def validate_config(kind: str, config) -> dict:
    """Return the normalized config (defaults applied). Unknown keys are rejected."""
    validate_kind(kind)
    if not isinstance(config, dict):
        raise ValidationError("config must be an object")
    allowed = {
        "git": {"url", "branch"},
        "jira": {"base_url", "projects"},
        "confluence": {"base_url", "spaces"},
        "s3": {"endpoint", "bucket", "prefix", "region", "include"},
    }[kind]
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise ValidationError(f"unknown config key(s): {', '.join(unknown)}")
    if kind == "git":
        return {"url": _url(config, "url"), "branch": _str(config, "branch", default="main")}
    if kind in ("jira", "confluence"):
        field = "projects" if kind == "jira" else "spaces"
        return {"base_url": _url(config, "base_url"), field: _str_list(config, field, True, _PROJECT_KEY)}
    out = {"bucket": _str(config, "bucket", required=True)}
    endpoint = _url(config, "endpoint", required=False)
    if endpoint:
        out["endpoint"] = endpoint
    for field in ("prefix", "region"):
        value = _str(config, field)
        if value:
            out[field] = value
    include = _str_list(config, "include", False)
    if include:
        out["include"] = include
    return out


def validate_secret(kind: str, secret) -> dict:
    """Check a secret object: known keys for the kind, string values. Values are never put in messages."""
    validate_kind(kind)
    if not isinstance(secret, dict):
        raise ValidationError("secret must be an object")
    unknown = sorted(set(secret) - SECRET_KEYS[kind])
    if unknown:
        raise ValidationError(f"unknown secret key(s): {', '.join(unknown)}")
    for key, value in secret.items():
        if not isinstance(value, str) or not value:
            raise ValidationError(f"secret.{key} must be a non-empty string")
    return secret


def validate_schedule(schedule) -> str | None:
    """None means manual only. Otherwise a 5-field cron expression (UTC)."""
    if schedule is None:
        return None
    if not isinstance(schedule, str) or len(schedule.split()) != 5:
        raise ValidationError("schedule must be a 5-field cron expression or null")
    from dagster._utils.schedules import is_valid_cron_string  # lazy: dagster is heavy

    if not is_valid_cron_string(schedule):
        raise ValidationError("schedule is not a valid cron expression")
    return schedule
