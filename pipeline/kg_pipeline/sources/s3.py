"""S3-compatible object storage as a document source: client builder and listing (pure parts, no graph writes).

boto3 is an optional dependency (extra `s3`). It is imported inside `build_client` only, so this module imports
without it.

Filters: the `include` globs are matched with `fnmatch.fnmatchcase` (case sensitive, `*` also matches `/`) on the
key relative to the prefix. The suffix check against `documents.SUFFIXES` is case insensitive. Keys that end with
`/` (directory placeholders) are ignored. Objects over KG_S3_MAX_BYTES (default 50 MiB, read at call time) are
reported as skipped and never downloaded.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from posixpath import splitext

from ..control.validate import ValidationError, _url
from .documents import SUFFIXES

DEFAULT_MAX_BYTES = 50 * 1024 * 1024
MAX_OBJECT_BYTES = DEFAULT_MAX_BYTES
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
MAX_ATTEMPTS = 3
_ANON = "anonymous"  # placeholder keys: with explicit keys boto3 never walks the credential chain


class RedirectRefused(Exception):
    """The server answered with a redirect. Redirects are not followed."""


def max_bytes() -> int:
    raw = os.environ.get("KG_S3_MAX_BYTES")
    try:
        return int(raw) if raw else DEFAULT_MAX_BYTES
    except ValueError:
        return DEFAULT_MAX_BYTES


def new_stats() -> dict:
    return {"listed": 0, "new": 0, "changed": 0, "unchanged": 0, "skipped": 0, "removed": 0, "bytes": 0}


def _refuse_redirect(response_dict=None, **_):
    status = (response_dict or {}).get("status_code") or 0
    if 300 <= status < 400:
        raise RedirectRefused(f"S3 endpoint answered with redirect {status}; redirects are not followed")


def build_client(config: dict, secret: dict | None, ui_origin: bool = False):
    """Return a boto3 S3 client.

    A ui-origin source without a secret is anonymous: signature UNSIGNED plus placeholder keys, so neither the
    AWS_* environment, a profile nor the instance chain is read. An env-origin source without a secret keeps the
    default credential chain. A custom endpoint uses path-style addressing and must be https (http only with
    KG_CONTROL_ALLOW_HTTP=1; the rule is the one of the control API validation).

    Redirects: botocore (urllib3) does not follow them itself, but its S3 region redirector would re-send to
    another host. A `response-received` hook raises on any 3xx first. That hook is only tested on a fake client;
    the behaviour against a real server is not verified.
    """
    endpoint = config.get("endpoint") or None
    if endpoint:
        try:
            _url({"endpoint": endpoint}, "endpoint")
        except ValidationError as e:
            raise ValueError(str(e)) from None
    try:
        import boto3  # optional dependency
        from botocore import UNSIGNED
        from botocore.config import Config
    except ImportError:
        raise RuntimeError("boto3 is not installed: install the s3 extra (pip install 'kg-pipeline[s3]')") from None
    secret = secret or {}
    key, sec = secret.get("access_key_id"), secret.get("secret_access_key")
    opts: dict = {"connect_timeout": CONNECT_TIMEOUT, "read_timeout": READ_TIMEOUT,
                  "retries": {"max_attempts": MAX_ATTEMPTS, "mode": "standard"}}
    if endpoint:
        opts["s3"] = {"addressing_style": "path"}
    creds: dict = {}
    if key and sec:
        creds = {"aws_access_key_id": key, "aws_secret_access_key": sec}
    elif ui_origin:
        opts["signature_version"] = UNSIGNED
        creds = {"aws_access_key_id": _ANON, "aws_secret_access_key": _ANON}
    client = boto3.client("s3", endpoint_url=endpoint, region_name=config.get("region") or None,
                          config=Config(**opts), **creds)
    client.meta.events.register("response-received", _refuse_redirect)
    return client


@dataclass
class S3Object:
    key: str
    size: int
    etag: str
    last_modified: str


@dataclass
class Listing:
    objects: list[S3Object] = field(default_factory=list)   # to process
    too_large: list[S3Object] = field(default_factory=list)  # exist, but are over the size limit
    complete: bool = False                                   # True only after the last page

    def stats(self) -> dict:
        s = new_stats()
        s["listed"] = len(self.objects) + len(self.too_large)
        s["skipped"] = len(self.too_large)
        return s


def list_objects(client, bucket: str, prefix: str, include: list[str], limit: int | None = None) -> Listing:
    """List the matching objects of a bucket. Any listing error raises: a partial listing is never returned."""
    limit = max_bytes() if limit is None else limit
    out = Listing()
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = client.list_objects_v2(**kw)
        for o in page.get("Contents") or []:
            key = o["Key"]
            if key.endswith("/") or not key.startswith(prefix):
                continue
            if splitext(key)[1].lower() not in SUFFIXES:
                continue
            if include and not any(fnmatch.fnmatchcase(key[len(prefix):], g) for g in include):
                continue
            rec = S3Object(key, int(o.get("Size") or 0), o.get("ETag") or "", str(o.get("LastModified") or ""))
            (out.too_large if rec.size > limit else out.objects).append(rec)
        if not page.get("IsTruncated"):
            out.complete = True
            return out
        token = page.get("NextContinuationToken")
        if not token:
            raise RuntimeError("truncated S3 listing without NextContinuationToken")
