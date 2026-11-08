"""Fix round 1 of the GraphRAG communities review: labelled lookups, skipped partitions, partial writes,
co-change paging, fail-closed ACL, safe env reads, per-batch report loading, chunk-only groups."""
from __future__ import annotations

import importlib
import re

import pytest

from kg_pipeline import communities
from kg_pipeline import community_reports as cr
from test_communities import LoaderWriter, StoreWriter, _n, _r, _t9_parts
from test_community_reports import StubEmb, World


def _labels(parts):
    return {i: ("Code" if key[0].startswith("repo:") else "Source") for key, (ids, _) in parts.items() for i in ids}


def _build_write(w, parts, labels=None, skipped=None, **kw):
    rows, stats = communities.build(parts, communities.load_existing(w), levels=2, backend="fallback",
                                    labels=labels, skipped_hashes=skipped, **kw)
    communities.write(w, rows)
    return rows, stats


# ---- H2: member lookups carry a label ---------------------------------------------------------------------

def test_member_queries_are_labelled_and_grouped_by_label():
    w = StoreWriter()
    parts = _t9_parts()
    _build_write(w, parts, labels=_labels(parts))
    member_qs = [q for q in w.queries if "IN_COMMUNITY]->(c)" in q and "MERGE" in q]
    assert member_qs
    for q in member_qs:
        assert re.search(r"MATCH \(m:(Code|Source|Domain|Requirement) \{id: pair\.m\}\)", q)
        assert not re.search(r"MATCH \(m \{", q)
    assert len({q for q in member_qs}) == 2  # one query text per label used


def test_pairs_of_a_label_query_hold_only_nodes_of_that_label():
    sent = []

    class W(StoreWriter):
        def _run(self, query, **p):
            if "MERGE (m)-[:IN_COMMUNITY]->(c)" in query:
                sent.append((re.search(r"\(m:(\w+) ", query).group(1), {x["m"] for x in p["pairs"]}))
            return super()._run(query, **p)

    parts = _t9_parts()
    labels = _labels(parts)
    _build_write(W(), parts, labels=labels)
    assert sent
    for label, ids in sent:
        assert all(labels[i] == label for i in ids)


def test_member_without_anchor_label_is_never_looked_up_unlabelled():
    w = StoreWriter()
    parts = _t9_parts()
    labels = _labels(parts)
    labels.pop("a0")
    _build_write(w, parts, labels=labels)
    assert not any(re.search(r"MATCH \(m \{", q) for q in w.queries)
    assert not any(m == "a0" for m, _ in w.member)


def test_schema_has_domain_and_requirement_id_index():
    text = "\n".join(communities.SCHEMA_STATEMENTS)
    assert "FOR (n:Domain) ON (n.id)" in text and "FOR (n:Requirement) ON (n.id)" in text


def test_load_partitions_returns_anchor_labels():
    nodes = [_n("a", "Symbol", repo="r"), _n("b", "Ticket", acl=["j"])]
    nodes[0]["labels"] = ["Symbol", "Code"]
    nodes[1]["labels"] = ["Ticket", "Source"]
    nodes.append({"id": "c", "labels": ["DomainConcept", "Domain"], "acl": ["j"]})
    _, stats = communities.load_partitions(LoaderWriter(nodes, []))
    assert stats["labels"] == {"a": "Code", "b": "Source", "c": "Domain"}


# ---- M1: skipped partitions are not stale ------------------------------------------------------------------

def test_skipped_partition_keeps_its_communities():
    w = StoreWriter()
    _build_write(w, _t9_parts())
    before = {i for i, c in w.comm.items() if c["acl"] == ["repo:r"]}
    assert before
    parts = _t9_parts()
    del parts[("repo:r",)]
    _, stats = _build_write(w, parts, skipped={communities.partition_hash(("repo:r",))})
    assert stats["stale_deleted"] == 0 and before <= set(w.comm)


def test_vanished_partition_without_skip_is_still_stale():
    w = StoreWriter()
    _build_write(w, _t9_parts())
    parts = _t9_parts()
    del parts[("repo:r",)]
    _, stats = _build_write(w, parts)
    assert stats["stale_deleted"] > 0 and not any(c["acl"] == ["repo:r"] for c in w.comm.values())


def test_partition_skipped_by_detect_keeps_its_communities(monkeypatch):
    w = StoreWriter()
    _build_write(w, _t9_parts())
    n = len(w.comm)
    monkeypatch.setenv("KG_COMMUNITY_MAX_NODES", "2")  # both partitions are above the cap
    _, stats = _build_write(w, _t9_parts())
    assert stats["skipped_too_large"] == 2 and stats["stale_deleted"] == 0 and len(w.comm) == n


def test_load_partitions_reports_skipped_hashes(monkeypatch):
    monkeypatch.setenv("KG_COMMUNITY_MAX_NODES", "5")
    nodes = [_n(f"a{i}", "Symbol", repo="big") for i in range(6)] + [_n("x1", "Symbol", repo="small")]
    _, stats = communities.load_partitions(LoaderWriter(nodes, []))
    assert stats["skipped_hashes"] == [communities.partition_hash(("repo:big",))]


def test_build_communities_passes_skipped_hashes(monkeypatch):
    w = StoreWriter()
    _build_write(w, _t9_parts())
    n = len(w.comm)
    parts = _t9_parts()
    del parts[("repo:r",)]
    h = communities.partition_hash(("repo:r",))
    monkeypatch.setattr(communities, "load_partitions",
                        lambda _w: (parts, {"partitions": 1, "skipped_too_large": 1, "skipped_hashes": [h]}))
    monkeypatch.setattr(communities, "load_member_hashes", lambda _w: {})
    stats = communities.build_communities(w, levels=2, backend="fallback")
    assert stats["stale_deleted"] == 0 and len(w.comm) == n


# ---- M2: a partial write is repaired -----------------------------------------------------------------------

def test_interrupted_write_is_repaired_on_the_next_run():
    class Crash(StoreWriter):
        def _run(self, query, **p):
            if "MERGE (m)-[:IN_COMMUNITY]->(c)" in query:
                raise RuntimeError("neo4j down")
            return super()._run(query, **p)

    w = Crash()
    with pytest.raises(RuntimeError):
        _build_write(w, _t9_parts())
    assert w.comm and not w.member  # communities with a hash but no members
    w.__class__ = StoreWriter
    rows, stats = _build_write(w, _t9_parts())
    assert stats["written"] == len(w.comm) and stats["unchanged"] == 0
    assert {m for m, _ in w.member} == {i for ids, _ in _t9_parts().values() for i in ids}


def test_lost_members_of_one_community_are_rewritten_only_there():
    w = StoreWriter()
    _build_write(w, _t9_parts())
    victim = next(c for _, c in sorted(w.member))
    w.member = {x for x in w.member if x[1] != victim}
    _, stats = _build_write(w, _t9_parts())
    assert stats["written"] == 1 and any(c == victim for _, c in w.member)


# ---- M3: co-change paging ----------------------------------------------------------------------------------

def test_cochange_pages_by_commit_without_post_limit_filter(monkeypatch):
    monkeypatch.setattr(communities, "COCHANGE_PAGE", 2)
    nodes = [_n(f, "File", repo="r") for f in ("f1", "f2", "f3")]
    commits = [(f"c{i}", ["f1"]) for i in range(1, 6)] + [("c6", ["f1", "f2"]), ("c7", ["f2", "f3"])]
    w = LoaderWriter(nodes, [], commits)
    parts, _ = communities.load_partitions(w)
    edges = {(u, v) for u, v, _ in parts[("repo:r",)][1]}
    assert edges == {("f1", "f2"), ("f2", "f3")}
    pages = [p for q, p in w.queries if "MODIFIES" in q]
    assert len(pages) >= 4
    assert not any("size(files)" in q for q, _ in w.queries if "MODIFIES" in q)


# ---- L1: invalid acl fails closed --------------------------------------------------------------------------

@pytest.mark.parametrize("acl", [[""], [None], ["", None], (None,)])
def test_acl_without_valid_group_is_not_public(acl):
    assert communities.partition_key({"acl": acl, "repo": "r"}) == ("__invalid__",)
    assert communities.partition_key({"acl": acl}) == ("__invalid__",)


def test_empty_acl_is_still_public_or_repo():
    assert communities.partition_key({"acl": []}) == ()
    assert communities.partition_key({"acl": [], "repo": "r"}) == ("repo:r",)
    assert communities.partition_key({"acl": [None, "g"]}) == ("g",)


# ---- L5: chunk-only groups ---------------------------------------------------------------------------------

def test_edges_of_chunk_only_groups_fold_to_the_owner():
    nodes = [_n("d", "Document", acl=["g"]), _n("c", "DomainConcept", acl=["g"]), _n("k", "Chunk", acl=["other"])]
    rels = [_r("k", "PART_OF", "d"), _r("k", "MENTIONS", "c")]
    parts, _ = communities.load_partitions(LoaderWriter(nodes, rels))
    assert ("other",) not in parts
    assert parts[("g",)] == (["c", "d"], [("c", "d", communities.EDGE_WEIGHTS["MENTIONS"])])


# ---- L3: env reads and per-batch loading -------------------------------------------------------------------

def test_bad_community_budget_does_not_break_import(monkeypatch):
    monkeypatch.setenv("COMMUNITY_BUDGET", "lots")
    try:
        importlib.reload(cr)
        assert cr.default_budget() == 100 and cr.budget_from_env() is None
    finally:
        monkeypatch.delenv("COMMUNITY_BUDGET")
        importlib.reload(cr)


def test_budget_reader(monkeypatch):
    monkeypatch.setenv("COMMUNITY_BUDGET", "7")
    assert cr.budget_from_env() == 7 and cr.default_budget() == 7
    monkeypatch.setenv("COMMUNITY_BUDGET", "0")
    assert cr.budget_from_env() == 0
    monkeypatch.delenv("COMMUNITY_BUDGET")
    assert cr.budget_from_env() is None and cr.default_budget() == 100


def test_report_inputs_are_loaded_per_batch(monkeypatch):
    seen = []
    real = cr.load_report_inputs
    monkeypatch.setattr(cr, "load_report_inputs", lambda w, ids: (seen.append(len(list(ids))), real(w, ids))[1])
    w = World(n=450)
    monkeypatch.delenv("COMMUNITY_LLM", raising=False)
    stats = cr.build_reports(w, llm=None, emb=StubEmb(), budget=0)
    assert stats["total"] == 450 and stats["written"] == 450
    assert len(seen) == 3 and max(seen) <= cr.LOAD_BATCH
