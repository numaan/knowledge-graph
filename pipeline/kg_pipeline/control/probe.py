"""Connection tests for sources, with an SSRF guard.

Before any outbound request the host is resolved and every address is checked. Loopback, link-local (this
includes the metadata address 169.254.169.254), private, multicast, reserved, unspecified and other non-global
addresses are refused, IPv4 and IPv6, and IPv4-mapped IPv6 is unwrapped first. `KG_CONTROL_ALLOW_PRIVATE=1`
turns the guard off. When refused, no request is made.

DNS rebinding: the Jira/Confluence client and git connect to the IP that was validated (TLS still checks the
original host name). What stays open: S3 through boto3 resolves the endpoint again itself, so a rebinding
answer between check and request is possible there. Redirects are not followed for Jira, Confluence and git.

Results are `{ok, message, details}`. Messages and details never hold secret values. Nothing is written to the graph database.
"""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from ..sources.atlassian import Atlassian

TIMEOUT = 15
ALLOW_PRIVATE_ENV = "KG_CONTROL_ALLOW_PRIVATE"


class Blocked(Exception):
    """The SSRF guard refused the host. The message is safe to return."""


def resolve(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})


def _category(ip: ipaddress._BaseAddress) -> str | None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    for name, bad in (("loopback", ip.is_loopback), ("link-local", ip.is_link_local),
                      ("unspecified", ip.is_unspecified), ("multicast", ip.is_multicast),
                      ("reserved", ip.is_reserved), ("private", ip.is_private)):
        if bad:
            return name
    return None if ip.is_global else "non-public"


def guard(url: str, resolver: Callable[[str], list[str]]) -> tuple[str, str, int]:
    """Return (host, validated ip, port). Raise Blocked when the host cannot be resolved or is refused."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise Blocked("URL has no host")
    port = parsed.port or (80 if parsed.scheme == "http" else 443)
    try:
        addresses = resolver(host)
    except OSError:
        raise Blocked(f"cannot resolve host {host}") from None
    if not addresses:
        raise Blocked(f"cannot resolve host {host}")
    allow = os.environ.get(ALLOW_PRIVATE_ENV) == "1"
    for address in addresses:
        try:
            kind = _category(ipaddress.ip_address(address.split("%")[0]))
        except ValueError:
            raise Blocked(f"cannot resolve host {host}") from None
        if kind and not allow:
            raise Blocked(f"SSRF guard: {host} resolves to a {kind} address ({address}); "
                          f"set {ALLOW_PRIVATE_ENV}=1 to allow it")
    return host, addresses[0], port


class _Pinned(http.client.HTTPSConnection):
    """Connects to a fixed IP and uses the URL host for TLS name checks and SNI."""
    pinned_ip = ""

    def connect(self):
        sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedPlain(http.client.HTTPConnection):
    pinned_ip = ""

    def connect(self):
        self.sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def pinned_fetch(ip: str, auth: str | None = None) -> Callable[[str], dict]:
    """A `fetch(url) -> json` that connects to `ip`. HTTP errors raise RuntimeError('... HTTP <code>')."""
    https = type("P", (_Pinned,), {"pinned_ip": ip})
    plain = type("Q", (_PinnedPlain,), {"pinned_ip": ip})
    context = ssl.create_default_context()

    class H(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(lambda host, **kw: https(host, context=context, **kw), req)

    class P(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(plain, req)

    opener = urllib.request.build_opener(H, P, _NoRedirect)

    def fetch(url: str) -> dict:
        headers = {"Accept": "application/json", **({"Authorization": auth} if auth else {})}
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RuntimeError(f"request failed: {getattr(e, 'reason', e)}") from None

    return fetch


def _run_git(args: list[str], env: dict, timeout: int) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["git", *args], env=env, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "", "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", "git ls-remote timed out"
    return p.returncode, p.stdout, p.stderr


@dataclass
class Prober:
    """Holds the replaceable parts so tests can use fakes. Defaults make real requests."""
    resolver: Callable[[str], list[str]] = resolve
    fetch: Callable[[str], dict] | None = None             # url -> JSON; None = pinned HTTP client
    git_runner: Callable[[list[str], dict, int], tuple[int, str, str]] = _run_git
    s3_client: Callable[[dict, dict], object] | None = None  # (config, secret) -> client with head_bucket

    def test(self, kind: str, config: dict, secret: dict | None) -> dict:
        secret = secret or {}
        try:
            if kind in ("jira", "confluence"):
                out = self._atlassian(kind, config, secret)
            elif kind == "git":
                out = self._git(config, secret)
            elif kind == "s3":
                out = self._s3(config, secret)
            else:
                out = _result(False, f"unknown kind {kind}")
        except Blocked as e:
            out = _result(False, str(e))
        except Exception as e:  # a tester must never raise or leak
            out = _result(False, f"{type(e).__name__}: {e}")
        return _scrub(out, secret)

    def _atlassian(self, kind: str, config: dict, secret: dict) -> dict:
        if not secret.get("email") or not secret.get("api_token"):
            return _result(False, "no secret configured (email and api_token are required)")
        base = config["base_url"]
        host, ip, _ = guard(base, self.resolver)
        auth = "Basic " + base64.b64encode(f"{secret['email']}:{secret['api_token']}".encode()).decode()
        fetch = self.fetch or pinned_fetch(ip, auth)
        client = Atlassian(base, secret["email"], secret["api_token"], fetch=fetch)
        path, params = ("/rest/api/3/myself", {}) if kind == "jira" else ("/wiki/rest/api/space", {"limit": 1})
        try:
            client.get(path, **params)
        except RuntimeError as e:
            return _result(False, _http_message(str(e)), {"host": host})
        return _result(True, f"connected to {host}", {"host": host})

    def _git(self, config: dict, secret: dict) -> dict:
        url, branch = config["url"], config.get("branch") or "main"
        host, ip, port = guard(url, self.resolver)
        env = {k: os.environ[k] for k in ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP") if k in os.environ}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="http:https", GIT_CONFIG_NOSYSTEM="1")
        args = ["-c", "http.followRedirects=false", "-c", f"http.curloptResolve={host}:{port}:{ip}"]
        if secret.get("token"):
            # Token goes through the environment, not the command line (visible in process lists).
            header = "Authorization: Basic " + base64.b64encode(f"x-access-token:{secret['token']}".encode()).decode()
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader", GIT_CONFIG_VALUE_0=header)
        code, stdout, stderr = self.git_runner([*args, "ls-remote", "--heads", url, f"refs/heads/{branch}"], env, TIMEOUT)
        details = {"host": host, "branch": branch}
        if code != 0:
            return _result(False, f"git ls-remote failed: {(stderr or stdout).strip()[:300]}", details)
        if not stdout.strip():
            return _result(False, f"branch {branch} not found", details)
        return _result(True, f"branch {branch} found on {host}", details)

    def _s3(self, config: dict, secret: dict) -> dict:
        details = {"bucket": config["bucket"]}
        if config.get("endpoint"):
            guard(config["endpoint"], self.resolver)  # boto3 resolves again itself: see module note
        if self.s3_client is not None:
            client = self.s3_client(config, secret)
        else:
            try:
                import boto3  # optional dependency, imported on use
                from botocore.config import Config
            except ImportError:
                return _result(False, "boto3 not installed")
            client = boto3.client(
                "s3", endpoint_url=config.get("endpoint"), region_name=config.get("region"),
                aws_access_key_id=secret.get("access_key_id"), aws_secret_access_key=secret.get("secret_access_key"),
                config=Config(connect_timeout=10, read_timeout=10, retries={"max_attempts": 1}))
        try:
            client.head_bucket(Bucket=config["bucket"])
        except Exception as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
            return _result(False, f"head_bucket failed: {code or type(e).__name__}", details)
        return _result(True, f"bucket {config['bucket']} is reachable", details)


def _result(ok: bool, message: str, details: dict | None = None) -> dict:
    return {"ok": ok, "message": message, "details": details or {}}


def _http_message(text: str) -> str:
    m = re.search(r"HTTP (\d{3})", text)
    if not m:
        return "request failed"
    code = int(m.group(1))
    if code in (401, 403):
        return f"authentication failed (HTTP {code}); check the email and API token"
    return f"request failed (HTTP {code})"


def _scrub(result: dict, secret: dict) -> dict:
    text = json.dumps(result)
    for value in secret.values():
        if isinstance(value, str) and len(value) >= 3:
            text = text.replace(value, "***").replace(json.dumps(value)[1:-1], "***")
            text = text.replace(base64.b64encode(value.encode()).decode(), "***")
    return json.loads(text)
