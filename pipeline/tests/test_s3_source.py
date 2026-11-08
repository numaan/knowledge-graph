"""S3 source: client builder and listing (no boto3, no network)."""
import sys
import types

import pytest

from kg_pipeline.sources import s3


class FakeClient:
    """Paged list_objects_v2 over a list of pages (each page is a list of object dicts)."""

    def __init__(self, pages, fail_at=None):
        self.pages, self.fail_at, self.calls, self.downloads = pages, fail_at, [], []

    def list_objects_v2(self, **kw):
        self.calls.append(kw)
        i = int(kw["ContinuationToken"]) if kw.get("ContinuationToken") else 0
        if self.fail_at == i:
            raise RuntimeError("boom")
        out = {"Contents": self.pages[i], "IsTruncated": i + 1 < len(self.pages)}
        if out["IsTruncated"]:
            out["NextContinuationToken"] = str(i + 1)
        return out

    def download_file(self, *a, **kw):
        self.downloads.append(a)


def obj(key, size=10, etag='"e"'):
    return {"Key": key, "Size": size, "ETag": etag, "LastModified": "2024-01-01T00:00:00Z"}


PAGES = [
    [obj("docs/"), obj("docs/a.md"), obj("other/x.md")],
    [obj("docs/b.pdf"), obj("docs/sub/c.md"), obj("docs/img.png"), obj("docs/A.MD")],
    [obj("docs/d.txt")],
]


def keys(listing):
    return [o.key for o in listing.objects]


def test_pages_prefix_include_and_suffix():
    c = FakeClient(PAGES)
    listing = s3.list_objects(c, "b", "docs/", ["*.md", "*.pdf"], 1000)
    assert keys(listing) == ["docs/a.md", "docs/b.pdf", "docs/sub/c.md"]  # A.MD: glob is case sensitive
    assert len(c.calls) == 3 and c.calls[1]["ContinuationToken"] == "1"
    assert all(k["Bucket"] == "b" and k["Prefix"] == "docs/" for k in c.calls)
    assert listing.complete is True


def test_empty_include_keeps_all_supported_suffixes():
    listing = s3.list_objects(FakeClient(PAGES), "b", "docs/", [], 1000)
    assert keys(listing) == ["docs/a.md", "docs/b.pdf", "docs/sub/c.md", "docs/A.MD", "docs/d.txt"]
    assert listing.objects[0].size == 10 and listing.objects[0].etag == '"e"'
    assert listing.objects[0].last_modified


def test_too_large_is_skipped_and_counted():
    pages = [[obj("a.md", 100), obj("b.md", 101), obj("c.pdf", 5000)]]
    listing = s3.list_objects(FakeClient(pages), "b", "", [], 100)
    assert keys(listing) == ["a.md"]
    assert [o.key for o in listing.too_large] == ["b.md", "c.pdf"]
    assert listing.stats()["skipped"] == 2 and listing.stats()["listed"] == 3


def test_max_bytes_env_read_at_call_time(monkeypatch):
    assert s3.max_bytes() == 50 * 1024 * 1024
    monkeypatch.setenv("KG_S3_MAX_BYTES", "50")
    pages = [[obj("a.md", 51), obj("b.md", 50)]]
    listing = s3.list_objects(FakeClient(pages), "b", "", [])
    assert keys(listing) == ["b.md"] and len(listing.too_large) == 1


def test_listing_error_raises_and_is_never_complete():
    with pytest.raises(RuntimeError):
        s3.list_objects(FakeClient(PAGES, fail_at=2), "b", "docs/", [], 1000)


def test_new_stats_keys():
    assert set(s3.new_stats()) == {"listed", "new", "changed", "unchanged", "skipped", "removed", "bytes"}
    assert set(s3.new_stats().values()) == {0}


# --- client builder -------------------------------------------------------------------------------------------

class FakeEvents:
    def __init__(self):
        self.handlers = {}

    def register(self, event, fn):
        self.handlers[event] = fn


def fake_boto(monkeypatch):
    seen = {}
    events = FakeEvents()
    boto3 = types.ModuleType("boto3")
    botocore = types.ModuleType("botocore")
    config = types.ModuleType("botocore.config")
    botocore.UNSIGNED = "UNSIGNED-SENTINEL"

    class Config:
        def __init__(self, **kw):
            self.kw = kw

    config.Config = Config

    def client(name, **kw):
        seen["name"], seen["kw"] = name, kw
        return types.SimpleNamespace(meta=types.SimpleNamespace(events=events))

    boto3.client = client
    for n, m in (("boto3", boto3), ("botocore", botocore), ("botocore.config", config)):
        monkeypatch.setitem(sys.modules, n, m)
    return seen, events


CFG = {"bucket": "b", "endpoint": "https://minio.example.com", "region": "eu-1"}


def test_signed_client_with_path_style(monkeypatch):
    seen, events = fake_boto(monkeypatch)
    s3.build_client(CFG, {"access_key_id": "AK", "secret_access_key": "SK"}, ui_origin=True)
    kw = seen["kw"]
    assert seen["name"] == "s3" and kw["endpoint_url"] == "https://minio.example.com"
    assert kw["aws_access_key_id"] == "AK" and kw["aws_secret_access_key"] == "SK"
    conf = kw["config"].kw
    assert conf["s3"] == {"addressing_style": "path"}
    assert "signature_version" not in conf
    assert conf["retries"]["max_attempts"] <= 5 and conf["connect_timeout"] and conf["read_timeout"]
    assert "response-received" in events.handlers


def test_ui_origin_without_secret_is_anonymous_and_ignores_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ENVKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ENVSECRET")
    seen, _ = fake_boto(monkeypatch)
    for secret in (None, {}, {"access_key_id": "", "secret_access_key": ""}):
        s3.build_client(CFG, secret, ui_origin=True)
        kw = seen["kw"]
        assert kw["config"].kw["signature_version"] == "UNSIGNED-SENTINEL"
        # explicit placeholder credentials switch the credential chain (env, profile, instance) off
        assert kw["aws_access_key_id"] not in (None, "ENVKEY") and kw["aws_secret_access_key"] != "ENVSECRET"


def test_env_origin_without_secret_uses_default_chain(monkeypatch):
    seen, _ = fake_boto(monkeypatch)
    s3.build_client(CFG, None, ui_origin=False)
    kw = seen["kw"]
    assert "aws_access_key_id" not in kw and "signature_version" not in kw["config"].kw


def test_no_endpoint_keeps_default_addressing(monkeypatch):
    seen, _ = fake_boto(monkeypatch)
    s3.build_client({"bucket": "b", "region": "eu-1"}, None, ui_origin=True)
    assert seen["kw"]["endpoint_url"] is None and "s3" not in seen["kw"]["config"].kw


def test_http_endpoint_needs_allow_http(monkeypatch):
    fake_boto(monkeypatch)
    cfg = dict(CFG, endpoint="http://minio:9000")
    with pytest.raises(ValueError, match="https"):
        s3.build_client(cfg, None, ui_origin=True)
    monkeypatch.setenv("KG_CONTROL_ALLOW_HTTP", "1")
    s3.build_client(cfg, None, ui_origin=True)


def test_redirect_hook_raises_on_3xx(monkeypatch):
    _, events = fake_boto(monkeypatch)
    s3.build_client(CFG, None, ui_origin=True)
    hook = events.handlers["response-received"]
    hook(response_dict={"status_code": 200})
    with pytest.raises(s3.RedirectRefused):
        hook(response_dict={"status_code": 307, "headers": {"location": "http://169.254.169.254/"}})


def test_missing_boto3_names_the_extra(monkeypatch):
    for n in ("boto3", "botocore", "botocore.config"):
        monkeypatch.setitem(sys.modules, n, None)  # import raises ImportError
    with pytest.raises(RuntimeError, match=r"kg-pipeline\[s3\]"):
        s3.build_client(CFG, None, ui_origin=True)
