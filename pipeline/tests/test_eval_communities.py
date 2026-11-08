"""Community metrics of kg-eval: pure helpers, the section runner on a fake graph, and the gates."""
from types import SimpleNamespace

from kg_pipeline import evaluate
from kg_pipeline.evaluate import acl_purity, eval_communities, gate_failures, global_hit, report, report_coverage


def test_acl_purity_mixed_community_is_below_one():
    rows = [{"id": "c1", "keys": [("jira:A",), ("jira:A",)]},
            {"id": "c2", "keys": [("jira:A",), ("jira:B",)]},
            {"id": "c3", "keys": [("repo:x",)]},
            {"id": "c4", "keys": [()]}]
    assert acl_purity(rows) == 0.75
    assert acl_purity(rows[:1]) == 1.0
    assert acl_purity([]) == 1.0
    assert acl_purity([{"id": "c", "keys": [(), ("repo:x",)]}]) == 0.0


def test_report_coverage():
    communities = ["c1", "c2", "c3", "c4"]
    reports = [{"community_id": "c1"}, {"community_id": "c2"}, {"community_id": "c3"}, {"community_id": "zz"}]
    assert report_coverage(communities, reports) == 0.75
    assert report_coverage([], reports) == 1.0


def test_global_hit():
    reports = [{"title": "Checkout flow", "summary": "Orders and payment", "findings": ["Refund handling"]},
               {"title": "Other", "summary": "nothing", "findings": []}]
    assert global_hit(["refund"], reports)
    assert global_hit(["nope", "PAYMENT"], reports)
    assert not global_hit(["nope"], reports)
    assert not global_hit(["checkout"], reports[1:])
    assert not global_hit([], reports)


class FakeWriter:
    """Answers the three queries of eval_communities by their text."""

    def __init__(self, communities, members, reports):
        self.communities, self.members, self.reports = communities, members, reports

    def _run(self, query, **params):
        if "IN_COMMUNITY" in query:
            rows = self.members
        elif "community_report_embedding" in query or "(r:CommunityReport)" in query:
            rows = self.reports
        else:
            rows = self.communities
        return SimpleNamespace(records=[dict(r) for r in rows])


class FakeEmb:
    def query(self, text):
        return [0.0]


def make(spec, communities=(), members=(), reports=()):
    return eval_communities(FakeWriter(list(communities), list(members), list(reports)), FakeEmb(), "bookshop", spec)


def test_eval_communities_no_communities_is_skipped():
    res = make({"cases": [{"question": "q", "expect_any": ["x"]}]})
    assert res["communities_created"] == 0
    assert res["skipped"]


def test_eval_communities_metrics():
    communities = [{"id": "c1", "level": 0, "acl": []}, {"id": "c2", "level": 0, "acl": []}]
    members = [{"cid": "c1", "acl": None, "repo": "bookshop"}, {"cid": "c1", "acl": [], "repo": "bookshop"},
               {"cid": "c2", "acl": ["jira:A"], "repo": None}, {"cid": "c2", "acl": ["jira:B"], "repo": None}]
    reports = [{"community_id": "c1", "title": "Catalog", "summary": "books", "findings": [], "score": 1.0}]
    res = make({"cases": [{"question": "what is sold", "expect_any": ["books"]},
                          {"question": "who pays", "expect_any": ["invoice"]}]}, communities, members, reports)
    assert res["communities_created"] == 2
    assert res["acl_purity"] == 0.5
    assert res["report_coverage"] == 0.5
    assert res["global_hits"] == 0.5
    assert [r["hit"] for r in res["rows"]] == [True, False]
    assert "skipped" not in res


def test_gates_communities():
    ok = {"communities_created": 3, "acl_purity": 1.0, "report_coverage": 0.5, "global_hits": 1.0, "rows": []}
    base = {"search": {"hit_at_k": 1.0, "mrr": 1.0}, "impact": {"cases": 0, "recall": 0.0}, "communities": ok}
    gates = {"min_communities": 2, "min_acl_purity": 1.0, "min_report_coverage": 0.8}
    assert gate_failures(base, gates) == ["report_coverage 0.5 < 0.8"]
    none = {**base, "communities": {"communities_created": 0, "skipped": "no communities", "rows": []}}
    assert gate_failures(none, gates) == ["communities 0 < 2"]
    assert gate_failures(none, {"min_acl_purity": 1.0, "min_report_coverage": 1.0}) == []
    assert gate_failures({k: v for k, v in base.items() if k != "communities"}, gates) == []


def test_report_unchanged_without_communities():
    res = {"repo": "r", "k": 8, "search": {"hit_at_k": 1.0, "hit_at_3": 1.0, "mrr": 1.0, "seconds_per_query": 0.1,
                                          "cases": 1, "rows": []},
           "impact": {"cases": 0, "recall": 0.0, "rows": []}, "requirements": {"cases": 0, "checked": 0, "accuracy": 0.0, "rows": []}}
    text = report("x", res)
    assert "ommunit" not in text
    with_c = report("x", {**res, "communities": {"communities_created": 2, "acl_purity": 1.0, "report_coverage": 1.0,
                                                 "global_hits": 0.5, "rows": [{"question": "q", "hit": False, "top": []}]}})
    assert with_c.startswith(text.rstrip("\n"))
    assert "## Communities" in with_c and "| ACL purity | 100% |" in with_c and "- q" in with_c
    skipped = report("x", {**res, "communities": {"communities_created": 0, "skipped": "no communities", "rows": []}})
    assert "skipped" in skipped


def test_run_section_only_when_in_spec(monkeypatch):
    monkeypatch.setattr(evaluate, "eval_search", lambda *a: {})
    monkeypatch.setattr(evaluate, "eval_impact", lambda *a: {})
    monkeypatch.setattr(evaluate, "eval_requirements", lambda *a: {})
    monkeypatch.setattr(evaluate, "eval_answers", lambda *a: {})
    monkeypatch.setattr(evaluate, "eval_communities", lambda *a: {"communities_created": 1})
    assert "communities" not in evaluate.run(None, None, {"repo": "r"})
    assert evaluate.run(None, None, {"repo": "r", "communities": {}})["communities"] == {"communities_created": 1}
