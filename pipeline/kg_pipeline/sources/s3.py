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
import hashlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from posixpath import splitext

from ..control.validate import ValidationError, _url
from .documents import SUFFIXES, _write_document

log = logging.getLogger("kg_pipeline.s3")

DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_OBJECTS = 500_000
DELETE_BATCH = 500  # ids per delete query: no query parameter grows with the size of the bucket
MAX_OBJECT_BYTES = DEFAULT_MAX_BYTES
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
MAX_ATTEMPTS = 3
_ANON = "anonymous"  # placeholder keys: with explicit keys boto3 never walks the credential chain


_REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-\d", re.ASCII)   # used with fullmatch; ASCII: no Unicode digits


class RedirectRefused(Exception):
    """The server answered with a redirect. Redirects are not followed.

    `region` is the value of the x-amz-bucket-region header of the answer. It is set for AWS only (no custom
    endpoint) and is not validated here; `call_with_region_hint` validates it before it is used.
    """

    def __init__(self, message: str, region: str | None = None):
        super().__init__(message)
        self.region = region


def max_bytes() -> int:
    raw = os.environ.get("KG_S3_MAX_BYTES")
    try:
        return int(raw) if raw else DEFAULT_MAX_BYTES
    except ValueError:
        return DEFAULT_MAX_BYTES


def max_objects() -> int:
    raw = os.environ.get("KG_S3_MAX_OBJECTS")
    try:
        value = int(raw) if raw else DEFAULT_MAX_OBJECTS
        return value if value >= 1 else DEFAULT_MAX_OBJECTS
    except ValueError:
        return DEFAULT_MAX_OBJECTS


def new_stats() -> dict:
    return {"listed": 0, "new": 0, "changed": 0, "unchanged": 0, "skipped": 0, "removed": 0, "bytes": 0}


def _redirect_hook(aws: bool):
    """`response-received` hook: refuse every 3xx. For AWS the region header of the answer is kept as a hint only;
    the Location header is never read and the host never changes."""
    def hook(response_dict=None, **_):
        response_dict = response_dict or {}
        status = response_dict.get("status_code") or 0
        if 300 <= status < 400:
            region = None
            if aws:
                headers = response_dict.get("headers") or {}
                region = next((str(v) for k, v in headers.items() if str(k).lower() == "x-amz-bucket-region"), None)
            raise RedirectRefused(f"S3 endpoint answered with redirect {status}; redirects are not followed", region)
    return hook


def call_with_region_hint(config: dict, secret: dict | None, ui_origin: bool, fn, client=None):
    """Run `fn(client)`. An AWS bucket (no custom endpoint) in another region answers with a redirect that carries
    the region. Then the client is rebuilt once with that region (only if it matches `_REGION`) and `fn` runs again.
    `fn` must be safe to repeat: the listing is its first step and fails before anything is written.
    Any other redirect, a custom endpoint, an invalid region or a second redirect raises RedirectRefused."""
    client = client or build_client(config, secret, ui_origin)
    try:
        return fn(client)
    except RedirectRefused as e:
        if config.get("endpoint"):
            raise
        hint = e.region or ""
        if not _REGION.fullmatch(hint) or hint == config.get("region"):
            raise RedirectRefused("S3 answered with a redirect: the bucket is in another region; "
                                  "set `region` in the source") from None
        log.info("bucket %s is in region %s: retrying once", config.get("bucket"), hint)
    try:
        return fn(build_client({**config, "region": hint}, secret, ui_origin))
    except RedirectRefused:
        raise RedirectRefused("S3 answered with a redirect again after the region change; "
                              "set `region` in the source") from None


def build_client(config: dict, secret: dict | None, ui_origin: bool = False):
    """Return a boto3 S3 client.

    A ui-origin source without a secret is anonymous: signature UNSIGNED plus placeholder keys, so neither the
    AWS_* environment, a profile nor the instance chain is read. An env-origin source without a secret keeps the
    default credential chain. A custom endpoint uses path-style addressing and must be https (http only with
    KG_CONTROL_ALLOW_HTTP=1; the rule is the one of the control API validation).

    Redirects: botocore (urllib3) does not follow them itself, but its S3 region redirector would re-send to
    another host. A `response-received` hook raises on any 3xx first. For AWS (no endpoint) the error carries the
    x-amz-bucket-region header; see `call_with_region_hint`. The hook is only tested on a fake client (the real
    botocore is not installed in the test environment); the behaviour against a real server is not verified.
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
    client.meta.events.register("response-received", _redirect_hook(not endpoint))
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
    cap = max_objects()
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
            if len(out.objects) + len(out.too_large) > cap:
                raise RuntimeError(f"S3 listing of bucket {bucket} with prefix {prefix!r} has more than {cap} "
                                   f"matching objects (KG_S3_MAX_OBJECTS); narrow the prefix or the include globs "
                                   f"of the source, or raise KG_S3_MAX_OBJECTS. Nothing was written or removed.")
        if not page.get("IsTruncated"):
            out.complete = True
            return out
        token = page.get("NextContinuationToken")
        if not token:
            raise RuntimeError("truncated S3 listing without NextContinuationToken")


# ---- sync -----------------------------------------------------------------------------------------------------
_STORED = ("MATCH (d:Document {collection: $name, source: 's3'}) RETURN d.id AS id, d.version AS version, "
           "d.s3_etag AS etag, d.s3_size AS size, d.s3_last_modified AS lm")


def _stored(w, name: str) -> dict[str, dict]:
    return {r["id"]: r for r in w._run(_STORED, name=name).records}


def _file_name(key: str) -> str:
    """Last path part of a key as a safe local file name (keeps the suffix, which read_document needs)."""
    base = re.sub(r"[^\w.\- ]", "_", key.replace("\\", "/").rsplit("/", 1)[-1])
    return "object" if base.strip(". ") == "" else base


def reconcile(w, name: str, listing: Listing, force: bool = False, stored: dict | None = None) -> int:
    """Remove the Documents (with Sections) of source `name` whose key is gone. Returns how many.

    Only a complete listing removes anything. Keys in `objects` and in `too_large` exist. The query filters on the
    source name and on source 's3', so documents of other sources and of local folders are never touched.
    """
    from ..reconcile import TooManyRemovals, _guard
    if not listing.complete:
        return 0
    stored = _stored(w, name) if stored is None else stored
    present = {f"s3:{name}:{o.key}" for o in listing.objects + listing.too_large}
    gone = sorted(set(stored) - present)
    del present
    if not gone:
        return 0
    try:
        _guard(len(gone), len(stored), f"S3 source {name}", force)   # before any deletion, from the stored total
    except TooManyRemovals as e:
        raise TooManyRemovals(f"{e}; to accept it, start a Full run of the source in the admin UI or run "
                              f"kg-s3 --force") from None
    removed = 0
    # Batches: a failure between two batches leaves a consistent graph; the next run finds the rest as gone.
    for i in range(0, len(gone), DELETE_BATCH):
        removed += w._run("MATCH (d:Document {collection: $name, source: 's3'}) WHERE d.id IN $ids "
                          "OPTIONAL MATCH (s:Section)-[:PART_OF]->(d) DETACH DELETE s, d RETURN count(DISTINCT d) AS n",
                          name=name, ids=gone[i:i + DELETE_BATCH]).records[0]["n"]
    return removed


def sync(w, client, name: str, config: dict, full: bool = False, force: bool = False, read=None) -> dict:
    """List the bucket, write new and changed objects as Documents, remove the Documents of deleted keys.

    An object whose etag, size and last-modified equal the stored values is not downloaded (not in mode full).
    A downloaded object whose content hash equals the stored version only gets the three stored fields refreshed.
    One object that fails is counted in `failed`; a listing error raises before anything is written.
    """
    from ..requirements import read_document  # Docling for binary formats
    read = read or read_document
    bucket, prefix, endpoint = config["bucket"], config.get("prefix") or "", config.get("endpoint") or ""
    listing = list_objects(client, bucket, prefix, config.get("include") or [])
    stats = {**listing.stats(), "documents": 0, "sections": 0, "failed": 0}
    stored = _stored(w, name)
    with tempfile.TemporaryDirectory(prefix="kg-s3-") as tmp:
        for i, o in enumerate(listing.objects):
            doc_id = f"s3:{name}:{o.key}"
            old = stored.get(doc_id)
            if old and not full and (old["etag"], old["size"], old["lm"]) == (o.etag, o.size, o.last_modified):
                stats["unchanged"] += 1
                continue
            try:
                folder = Path(tmp) / str(i)
                folder.mkdir()
                path = folder / _file_name(o.key)
                client.download_file(Bucket=bucket, Key=o.key, Filename=str(path))
                data = path.read_bytes()
                stats["bytes"] += len(data)
                version = hashlib.sha256(data).hexdigest()[:16]
                if old and not full and old["version"] == version:
                    w._run("MATCH (d:Document {id: $id}) SET d.s3_etag = $etag, d.s3_size = $size, "
                           "d.s3_last_modified = $lm", id=doc_id, etag=o.etag, size=o.size, lm=o.last_modified)
                    stats["unchanged"] += 1
                    continue
                markdown = read(path)
                rel = o.key[len(prefix):]
                title = next((l.lstrip("# ").strip() for l in markdown.splitlines() if l.startswith("# ")), path.stem)
                doc = {"id": doc_id, "name": rel.rsplit("/", 1)[-1], "title": title, "path": rel,
                       "collection": name, "version": version, "format": splitext(o.key)[1].lower().lstrip("."),
                       "source": "s3", "acl": [f"docs:{name}"], "url": f"s3://{bucket}/{o.key}",
                       "s3_etag": o.etag, "s3_size": o.size, "s3_last_modified": o.last_modified,
                       "s3_endpoint": endpoint, "source_updated_at": o.last_modified}
                stats["sections"] += _write_document(w, doc, markdown)
                stats["documents"] += 1
                stats["changed" if old else "new"] += 1
            except Exception as e:  # one broken object must not stop the bucket
                log.warning("cannot process s3://%s/%s: %s", bucket, o.key, e)
                stats["failed"] += 1
    stats["removed"] = reconcile(w, name, listing, force, stored)
    from .history import link_references
    return {**stats, "references": link_references(w)}


# ---- CLI --------------------------------------------------------------------------------------------------------
def default_name(bucket: str, prefix: str) -> str:
    """Source name from bucket and prefix: lower case, other characters become `-`, at most 63 characters."""
    slug = re.sub(r"[^a-z0-9]+", "-", f"{bucket}-{prefix}".lower()).strip("-")[:63].strip("-")
    return slug or "s3"


def dry_run(w, client, name: str, config: dict, full: bool = False) -> dict:
    """Report what sync would do. Lists the bucket and reads the stored Documents; writes nothing."""
    listing = list_objects(client, config["bucket"], config.get("prefix") or "", config.get("include") or [])
    stats = {**listing.stats(), "dry_run": True}
    stored = _stored(w, name)
    for o in listing.objects:
        old = stored.get(f"s3:{name}:{o.key}")
        if not old:
            stats["new"] += 1
        elif not full and (old["etag"], old["size"], old["lm"]) == (o.etag, o.size, o.last_modified):
            stats["unchanged"] += 1
        else:
            stats["changed"] += 1  # an upper bound: a download with an equal content hash is counted unchanged
    if listing.complete:
        present = {f"s3:{name}:{o.key}" for o in listing.objects + listing.too_large}
        stats["removed"] = len(set(stored) - present)
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(
        description="Sync an S3-compatible bucket into the graph as Documents. The CLI is a local operator tool: it "
                    "uses ui_origin=False semantics, so AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (and the default "
                    "boto3 credential chain) are used. Neo4j: NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD.")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="", help="key prefix (default: whole bucket)")
    ap.add_argument("--endpoint", help="custom endpoint URL (https; MinIO, Ceph, ...)")
    ap.add_argument("--include", action="append", default=[], metavar="GLOB",
                    help="glob on the key relative to the prefix; repeatable (default: all supported suffixes)")
    ap.add_argument("--name", help="source name, [a-z0-9][a-z0-9-]{0,62} (default: derived from bucket and prefix)")
    ap.add_argument("--region")
    ap.add_argument("--full", action="store_true", help="download and rewrite every object, ignore stored etags")
    ap.add_argument("--force", action="store_true", help="remove deleted objects even when it is more than half")
    ap.add_argument("--dry-run", action="store_true", help="list and report what would change; write nothing")
    args = ap.parse_args(argv)
    name = args.name or default_name(args.bucket, args.prefix)
    config = {"bucket": args.bucket, "prefix": args.prefix, "include": args.include, "endpoint": args.endpoint or ""}
    if args.region:
        config["region"] = args.region
    try:
        from ..control.validate import validate_name
        validate_name(name)
        env = os.environ
        secret = ({"access_key_id": env["AWS_ACCESS_KEY_ID"], "secret_access_key": env["AWS_SECRET_ACCESS_KEY"]}
                  if env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY") else None)
        client = build_client(config, secret, ui_origin=False)   # fail fast on a bad config or a missing boto3
        from ..graph.neo4j_writer import Neo4jWriter
        with Neo4jWriter(env.get("NEO4J_URI", "bolt://localhost:7687"), env.get("NEO4J_USER", "neo4j"),
                         env["NEO4J_PASSWORD"]) as w:
            if args.dry_run:
                res = call_with_region_hint(config, secret, False, lambda c: dry_run(w, c, name, config, args.full),
                                            client)
            else:
                res = call_with_region_hint(config, secret, False,
                                            lambda c: sync(w, c, name, config, full=args.full, force=args.force), client)
    except Exception as e:
        print(f"kg-s3: {e}", file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
