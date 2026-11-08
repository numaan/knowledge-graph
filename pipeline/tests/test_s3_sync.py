"""S3 source: sync, graph model and reconcile with an in-memory writer and a fake client (no boto3, no network)."""
import hashlib
from pathlib import Path

import pytest

from kg_pipeline import reconcile
from kg_pipeline.sources import s3

BODY = b"# Title\n\nsome words in a section of the document here\n"


def obj(key, size=10, etag='"e"'):
    return {"Key": key, "Size": size, "ETag": etag, "LastModified": "2024-01-01T00:00:00Z"}


class Res:
    def __init__(self, records):
        self.records = records


class MemWriter:
    """In-memory stand-in for Neo4jWriter: answers the queries of s3.sync, _write_document and link_mentions."""

    def __init__(self):
        self.docs, self.sections, self.queries, self.writes = {}, {}, [], []

    def upsert(self, labels, rows):
        if labels == ("Document",):
            for r in rows:
                self.writes.append(r["id"])
                self.docs[r["id"]] = {**self.docs.get(r["id"], {}), **r}
        elif labels == ("Section",):
            for r in rows:
                self.sections[r["id"]] = r

    def link(self, *a, **k):
        return 0

    def _run(self, q, **p):
        self.queries.append((q, p))
        if "RETURN d.acl AS acl" in q:
            return Res([{"acl": self.docs[p["id"]]["acl"]}])
        if "RETURN d.id AS id" in q and "d.version" in q:
            return Res([{"id": i, "version": d.get("version"), "etag": d.get("s3_etag"), "size": d.get("s3_size"),
                         "lm": d.get("s3_last_modified")} for i, d in self.docs.items()
                        if d.get("collection") == p["name"] and d.get("source") == "s3"])
        if "SET d.s3_etag" in q:
            self.docs[p["id"]].update(s3_etag=p["etag"], s3_size=p["size"], s3_last_modified=p["lm"])
            return Res([])
        if "DETACH DELETE s, d" in q:
            gone = [i for i, d in self.docs.items() if d.get("collection") == p["name"] and d.get("source") == "s3"
                    and i in p["ids"]]
            for i in gone:
                del self.docs[i]
                self.sections = {k: v for k, v in self.sections.items() if not k.startswith(i + "#")}
            return Res([{"n": len(gone)}])
        if "NOT s.id IN" in q:
            return Res([])
        return Res([{"n": 0}])


class Client:
    """One page of objects; download_file writes `content` (default BODY) to the target path."""

    def __init__(self, objects, content=None, bad=(), fail_list=False):
        self.objects, self.content, self.bad, self.fail_list = objects, content or {}, set(bad), fail_list
        self.downloads, self.files = [], []

    def list_objects_v2(self, **kw):
        if self.fail_list:
            raise RuntimeError("boom")
        return {"Contents": self.objects, "IsTruncated": False}

    def download_file(self, Bucket, Key, Filename):
        self.downloads.append((Bucket, Key))
        self.files.append(Path(Filename))
        if Key in self.bad:
            raise OSError("no access")
        Path(Filename).write_bytes(self.content.get(Key, BODY))


CFG = {"bucket": "b", "prefix": "docs/", "include": [], "endpoint": "https://s3.example"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.delenv("KG_S3_MAX_BYTES", raising=False)
    monkeypatch.setattr("kg_pipeline.sources.history.link_references", lambda w: 0)


def test_new_object_graph_model():
    w, c = MemWriter(), Client([obj("docs/a.md", size=51, etag='"x1"')])
    stats = s3.sync(w, c, "s", CFG)
    doc = w.docs["s3:s:docs/a.md"]
    assert doc["name"] == "a.md" and doc["path"] == "a.md" and doc["url"] == "s3://b/docs/a.md"
    assert doc["source"] == "s3" and doc["collection"] == "s" and doc["acl"] == ["docs:s"]
    assert doc["version"] == hashlib.sha256(BODY).hexdigest()[:16]
    assert (doc["s3_etag"], doc["s3_size"], doc["s3_last_modified"], doc["s3_endpoint"]) == (
        '"x1"', 51, "2024-01-01T00:00:00Z", "https://s3.example")
    assert doc["title"] == "Title" and len(w.sections) == 1
    assert (stats["new"], stats["changed"], stats["unchanged"], stats["listed"], stats["failed"]) == (1, 0, 0, 1, 0)
    assert stats["bytes"] == len(BODY)


def test_same_key_in_two_sources_gives_two_ids():
    w = MemWriter()
    for name in ("s1", "s2"):
        s3.sync(w, Client([obj("docs/a.md")]), name, CFG)
    assert sorted(w.docs) == ["s3:s1:docs/a.md", "s3:s2:docs/a.md"]


def test_pdf_goes_through_read_document_and_md_is_read_directly(monkeypatch):
    from kg_pipeline import requirements
    real, seen = requirements.read_document, []

    def read(p):   # Docling stand-in for binary formats; md and txt use the real reader
        if p.suffix == ".pdf":
            seen.append(p.name)
            return "# Pdf\n\nwords words words words words\n"
        return real(p)
    monkeypatch.setattr(requirements, "read_document", read)
    w = MemWriter()
    s3.sync(w, Client([obj("docs/a.md"), obj("docs/b.pdf")]), "s", CFG)
    assert seen == ["b.pdf"]
    assert set(w.docs) == {"s3:s:docs/a.md", "s3:s:docs/b.pdf"}
    assert w.docs["s3:s:docs/a.md"]["title"] == "Title" and w.docs["s3:s:docs/b.pdf"]["title"] == "Pdf"


def test_failed_object_does_not_stop_the_others():
    def read(p):
        if p.name == "bad.md":
            raise ValueError("cannot convert")
        return "# T\n\nwords words words words words\n"
    c = Client([obj("docs/bad.md"), obj("docs/ok.md"), obj("docs/nodl.md")], bad=["docs/nodl.md"])
    w = MemWriter()
    stats = s3.sync(w, c, "s", CFG, read=read)
    assert set(w.docs) == {"s3:s:docs/ok.md"} and stats["failed"] == 2 and stats["new"] == 1


def test_listing_error_aborts_and_removes_nothing():
    w = MemWriter()
    s3.sync(w, Client([obj("docs/a.md")]), "s", CFG)
    with pytest.raises(RuntimeError):
        s3.sync(w, Client([], fail_list=True), "s", CFG)
    assert set(w.docs) == {"s3:s:docs/a.md"}


def stored(w, etag='"e"', size=10):
    s3.sync(w, Client([obj("docs/a.md", size=size, etag=etag)]), "s", CFG)


def test_unchanged_object_is_not_downloaded():
    w = MemWriter()
    stored(w)
    c = Client([obj("docs/a.md")])
    stats = s3.sync(w, c, "s", CFG)
    assert c.downloads == [] and stats["unchanged"] == 1 and stats["bytes"] == 0 and stats["new"] == 0


def test_full_mode_ignores_the_stored_object_skip():
    w = MemWriter()
    stored(w)
    writes = len(w.writes)
    c = Client([obj("docs/a.md")])
    stats = s3.sync(w, c, "s", CFG, full=True)
    assert c.downloads == [("b", "docs/a.md")] and stats["changed"] == 1 and len(w.writes) == writes + 1


def test_changed_etag_same_content_refreshes_only_the_stored_fields():
    w = MemWriter()
    stored(w)
    writes = len(w.writes)
    c = Client([obj("docs/a.md", size=11, etag='"new"')])
    stats = s3.sync(w, c, "s", CFG)
    assert len(c.downloads) == 1 and len(w.writes) == writes and stats["unchanged"] == 1 and stats["bytes"] > 0
    assert w.docs["s3:s:docs/a.md"]["s3_etag"] == '"new"' and w.docs["s3:s:docs/a.md"]["s3_size"] == 11


def test_changed_content_is_written_again():
    w = MemWriter()
    stored(w)
    c = Client([obj("docs/a.md", etag='"new"')], content={"docs/a.md": b"# Other\n\nnew words in the document body text\n"})
    stats = s3.sync(w, c, "s", CFG)
    assert stats["changed"] == 1 and stats["new"] == 0 and w.docs["s3:s:docs/a.md"]["title"] == "Other"


def test_temp_dir_is_removed_and_hostile_keys_stay_inside():
    c = Client([obj("docs/../../evil.md"), obj("docs/..\\x\\y.md"), obj("docs/ok.md")])
    s3.sync(MemWriter(), c, "s", CFG)
    assert len(c.files) == 3
    roots = {f.parent.parent for f in c.files}
    assert len(roots) == 1   # one temp root; every file is exactly one folder below it
    assert not roots.pop().exists()
    c2 = Client([obj("/etc/passwd.md")])
    s3.sync(MemWriter(), c2, "s", {**CFG, "prefix": ""})
    assert c2.files[0].name == "passwd.md" and c2.files[0].parent.parent.name != "etc"


def test_temp_dir_is_removed_after_a_failure():
    c = Client([obj("docs/a.md")], bad=["docs/a.md"])
    s3.sync(MemWriter(), c, "s", CFG)
    assert not c.files[0].parent.exists()


# ---- reconcile --------------------------------------------------------------------------------------------------
def seed(w, name, keys, source="s3"):
    pre = "s3" if source == "s3" else "doc"
    for k in keys:
        w.docs[f"{pre}:{name}:{k}"] = {"id": f"{pre}:{name}:{k}", "collection": name, "source": source, "version": "v"}
        w.sections[f"{pre}:{name}:{k}#s0"] = {}


def test_reconcile_removes_gone_keys_with_sections_and_spares_other_sources():
    w = MemWriter()
    seed(w, "s", ["docs/a.md", "docs/gone.md"])
    seed(w, "other", ["docs/gone.md"])
    seed(w, "s", ["docs/gone.md"], source="files")   # a local folder with the same name
    listing = s3.Listing([s3.S3Object("docs/a.md", 1, "e", "m")], [], True)
    assert s3.reconcile(w, "s", listing) == 1
    assert "s3:s:docs/gone.md" not in w.docs and "s3:s:docs/a.md" in w.docs
    assert "s3:other:docs/gone.md" in w.docs and "doc:s:docs/gone.md" in w.docs
    assert "s3:s:docs/gone.md#s0" not in w.sections
    q = [q for q, _ in w.queries if "DETACH DELETE" in q][0]
    assert "source: 's3'" in q and "collection: $name" in q


def test_reconcile_keeps_too_large_keys():
    w = MemWriter()
    seed(w, "s", ["docs/big.md"])
    listing = s3.Listing([], [s3.S3Object("docs/big.md", 10**9, "e", "m")], True)
    assert s3.reconcile(w, "s", listing) == 0 and "s3:s:docs/big.md" in w.docs


def test_reconcile_never_runs_on_an_incomplete_listing():
    w = MemWriter()
    seed(w, "s", ["docs/a.md"])
    assert s3.reconcile(w, "s", s3.Listing([], [], False)) == 0
    assert "s3:s:docs/a.md" in w.docs and not [q for q, _ in w.queries if "DETACH DELETE" in q]


def test_reconcile_guard_and_force():
    w = MemWriter()
    seed(w, "s", [f"docs/{i}.md" for i in range(30)])
    listing = s3.Listing([s3.S3Object("docs/0.md", 1, "e", "m")], [], True)
    with pytest.raises(reconcile.TooManyRemovals, match=r"Full run.*kg-s3 --force"):
        s3.reconcile(w, "s", listing)
    assert len(w.docs) == 30
    assert s3.reconcile(w, "s", listing, force=True) == 29 and len(w.docs) == 1


def test_sync_reconciles_after_a_complete_listing_and_counts_removed():
    w = MemWriter()
    seed(w, "s", ["docs/old.md"])
    stats = s3.sync(w, Client([obj("docs/a.md")]), "s", CFG)
    assert stats["removed"] == 1 and set(w.docs) == {"s3:s:docs/a.md"}


# ---- scale: batched deletes and listing cap -----------------------------------------------------------------------
def test_reconcile_deletes_in_constant_size_batches(monkeypatch):
    monkeypatch.setattr(s3, "DELETE_BATCH", 10)
    w = MemWriter()
    seed(w, "s", [f"docs/{i}.md" for i in range(45)])
    listing = s3.Listing([s3.S3Object(f"docs/{i}.md", 1, "e", "m") for i in range(40)], [], True)
    assert s3.reconcile(w, "s", listing) == 5
    deletes = [p for q, p in w.queries if "DETACH DELETE" in q]
    assert len(w.docs) == 40 and all(len(p["ids"]) <= 10 for p in deletes)
    big = s3.Listing([], [], True)
    w2 = MemWriter()
    seed(w2, "s", [f"docs/{i}.md" for i in range(45)])
    assert s3.reconcile(w2, "s", big, force=True) == 45 and not w2.docs
    assert max(len(p["ids"]) for q, p in w2.queries if "DETACH DELETE" in q) <= 10


def test_reconcile_guard_runs_before_any_batch_is_deleted(monkeypatch):
    monkeypatch.setattr(s3, "DELETE_BATCH", 5)
    w = MemWriter()
    seed(w, "s", [f"docs/{i}.md" for i in range(30)])
    with pytest.raises(reconcile.TooManyRemovals):
        s3.reconcile(w, "s", s3.Listing([], [], True))
    assert len(w.docs) == 30 and not [q for q, _ in w.queries if "DETACH DELETE" in q]


def test_reconcile_partial_failure_is_finished_by_the_next_run(monkeypatch):
    monkeypatch.setattr(s3, "DELETE_BATCH", 5)
    w = MemWriter()
    seed(w, "s", [f"docs/{i}.md" for i in range(30)] + ["docs/keep.md"])
    listing = s3.Listing([s3.S3Object("docs/keep.md", 1, "e", "m")], [], True)
    real, calls = w._run, []

    def flaky(q, **p):
        if "DETACH DELETE" in q:
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("connection lost")
        return real(q, **p)
    w._run = flaky
    with pytest.raises(RuntimeError):
        s3.reconcile(w, "s", listing, force=True)
    assert 0 < len(w.docs) < 31
    w._run = real
    s3.reconcile(w, "s", listing, force=True)
    assert set(w.docs) == {"s3:s:docs/keep.md"}


def page_client(n, per_page=1000):
    class C:
        def list_objects_v2(self, **kw):
            start = int(kw.get("ContinuationToken", 0))
            end = min(start + per_page, n)
            out = {"Contents": [obj(f"docs/{i}.md") for i in range(start, end)], "IsTruncated": end < n}
            if end < n:
                out["NextContinuationToken"] = str(end)
            return out
    return C()


def test_listing_over_the_cap_raises_a_clear_error(monkeypatch):
    monkeypatch.setenv("KG_S3_MAX_OBJECTS", "2500")
    with pytest.raises(RuntimeError) as e:
        s3.list_objects(page_client(5000), "b", "docs/", [])
    assert "KG_S3_MAX_OBJECTS" in str(e.value) and "docs/" in str(e.value)
    assert len(s3.list_objects(page_client(2500), "b", "docs/", []).objects) == 2500


def test_listing_over_the_cap_writes_and_removes_nothing(monkeypatch):
    monkeypatch.setenv("KG_S3_MAX_OBJECTS", "5")
    w = MemWriter()
    seed(w, "s", ["docs/old.md"])
    with pytest.raises(RuntimeError):
        s3.sync(w, page_client(50, 10), "s", CFG)
    assert set(w.docs) == {"s3:s:docs/old.md"} and not w.writes
    assert not [q for q, _ in w.queries if "DETACH DELETE" in q]


def test_max_objects_default_and_bad_value(monkeypatch):
    monkeypatch.delenv("KG_S3_MAX_OBJECTS", raising=False)
    assert s3.max_objects() == 500000
    for bad in ("abc", "0", "-5"):
        monkeypatch.setenv("KG_S3_MAX_OBJECTS", bad)
        assert s3.max_objects() == 500000
    monkeypatch.setenv("KG_S3_MAX_OBJECTS", "1")
    assert s3.max_objects() == 1
