"""Fernet encryption of source secrets. The key comes from KG_SECRET_KEY."""
import json
import os

from cryptography.fernet import Fernet, InvalidToken

KEY_ENV = "KG_SECRET_KEY"


class SecretKeyError(RuntimeError):
    """KG_SECRET_KEY is missing or invalid, or a token cannot be decrypted with it."""


def _fernet() -> Fernet:
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise SecretKeyError(f"{KEY_ENV} is not set; it is required to store or read source secrets")
    try:
        return Fernet(key.encode())
    except ValueError as e:
        raise SecretKeyError(f"{KEY_ENV} is not a valid Fernet key (32 url-safe base64 bytes)") from e


def check_key() -> None:
    """Raise SecretKeyError when the key is missing or invalid (used at start-up)."""
    _fernet()


def encrypt(secret: dict) -> str:
    return _fernet().encrypt(json.dumps(secret, sort_keys=True).encode()).decode()


def decrypt(token: str) -> dict:
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except InvalidToken as e:
        raise SecretKeyError(f"stored secret cannot be decrypted with {KEY_ENV}") from e


def secret_keys(secret: dict | None) -> list[str]:
    return sorted(secret) if secret else []
