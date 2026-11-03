"""Sign-in, sign-out and password reset for customers."""
import hashlib
import secrets

from models import Customer
from services.email import send_password_reset

SESSIONS: dict[str, str] = {}
RESET_TOKENS: dict[str, str] = {}


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def login(customer: Customer, password: str) -> str:
    """Check the password and open a session; returns the session token."""
    if hash_password(password) != customer.password_hash:
        raise PermissionError("wrong email or password")
    token = secrets.token_urlsafe(16)
    SESSIONS[token] = customer.email
    return token


def logout(token: str) -> None:
    """End the session of the signed-in customer."""
    SESSIONS.pop(token, None)


def request_password_reset(customer: Customer) -> None:
    """Email a one-time reset link to a customer who forgot the password."""
    token = secrets.token_urlsafe(16)
    RESET_TOKENS[token] = customer.email
    send_password_reset(customer.email, token)


def reset_password(customer: Customer, token: str, new_password: str) -> None:
    if RESET_TOKENS.pop(token, None) != customer.email:
        raise PermissionError("invalid or used reset token")
    customer.password_hash = hash_password(new_password)
