"""Keycloak JWT validation (JWKS) and the `kg-admins` group check.

Environment: KG_OIDC_ISSUER (unset = local mode), KG_OIDC_AUDIENCE (checked when set),
KG_OIDC_GROUPS_CLAIM (default `groups`, dotted paths work), KG_CONTROL_ADMIN_GROUP (default
`kg-admins`), KG_OIDC_JWKS_URL (optional;
default `<issuer>/protocol/openid-connect/certs`).
"""
import os
from dataclasses import dataclass
from typing import Callable

import jwt
from fastapi import Depends, HTTPException, Request

ADMIN_GROUP = "kg-admins"


@dataclass
class User:
    name: str
    is_admin: bool


class Authenticator:
    def __init__(self, issuer: str = "", audience: str = "", groups_claim: str = "groups", jwks_url: str = "",
                 key_resolver: Callable[[str], object] | None = None, admin_group: str = ADMIN_GROUP):
        self.admin_group = admin_group or ADMIN_GROUP
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.groups_claim = groups_claim or "groups"
        self.jwks_url = jwks_url or (f"{self.issuer}/protocol/openid-connect/certs" if self.issuer else "")
        self._resolver = key_resolver
        self._jwks = None

    @classmethod
    def from_env(cls, key_resolver: Callable[[str], object] | None = None) -> "Authenticator":
        return cls(os.environ.get("KG_OIDC_ISSUER", ""), os.environ.get("KG_OIDC_AUDIENCE", ""),
                   os.environ.get("KG_OIDC_GROUPS_CLAIM", "groups"), os.environ.get("KG_OIDC_JWKS_URL", ""),
                   key_resolver, os.environ.get("KG_CONTROL_ADMIN_GROUP", "").strip())

    @property
    def local(self) -> bool:
        return not self.issuer

    def _key(self, token: str):
        if self._resolver:
            return self._resolver(token)
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(self.jwks_url)
        return self._jwks.get_signing_key_from_jwt(token).key

    def authenticate(self, token: str | None) -> User:
        """Return the caller. Raise HTTPException 401 for a missing or invalid token."""
        if self.local:
            return User("local", True)
        if not token:
            raise HTTPException(401, "missing bearer token")
        try:
            claims = jwt.decode(
                token, self._key(token), algorithms=["RS256", "RS384", "RS512", "ES256", "ES384"],
                issuer=self.issuer, audience=self.audience or None,
                options={"require": ["exp", "iss"], "verify_aud": bool(self.audience)}, leeway=30)
        except Exception as e:  # signature, expiry, issuer, audience, JWKS fetch
            raise HTTPException(401, "invalid token") from e
        name = claims.get("preferred_username") or claims.get("email") or claims.get("sub") or "unknown"
        return User(str(name), self.admin_group in _groups(claims, self.groups_claim))


def _groups(claims: dict, path: str) -> list[str]:
    cur = claims
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return []
        cur = cur[part]
    items = cur if isinstance(cur, list) else [cur]
    return [str(g).lstrip("/") for g in items]


def _bearer(request: Request) -> str | None:
    head = request.headers.get("authorization", "")
    scheme, _, value = head.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def require_user(request: Request) -> User:
    return request.app.state.auth.authenticate(_bearer(request))


def require_admin(request: Request, user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, f"group {request.app.state.auth.admin_group} is required")
    return user
