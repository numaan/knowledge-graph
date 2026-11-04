import gzip
import inspect
import json
import os
import random
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from kg_pipeline import backup, telemetry
from kg_pipeline.eval_answer import cited, eval_answers, facts_by_string, score_case, split_id
from kg_pipeline.evaluate import gate_failures
from kg_pipeline.fake_embed import serve, tokens, vector
from kg_pipeline.mcp_client import MCPClient, _sse_messages

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # pipeline/, for the scale package
from scale.generate import build_repo, tickets  # noqa: E402

NEEDS_NEO4J = pytest.mark.skipif(not os.environ.get("NEO4J_PASSWORD"), reason="NEO4J_PASSWORD not set")


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def test_fake_embedder_is_deterministic_and_lexical():
    a, b = vector("refund a cancelled order", 128), vector("refund_order: give the money back for an order", 128)
    assert a == vector("refund a cancelled order", 128)
    assert abs(_cos(a, a) - 1) < 1e-9
    assert _cos(a, b) > _cos(a, vector("hold books in the warehouse", 128))
    assert tokens("Instruct: retrieve code\nQuery: addToCart items") == ["add", "cart", "item"]
    assert len(vector("", 16)) == 16 and _cos(vector("", 16), vector("", 16)) == 1


def test_fake_embedder_serves_openai_format():
    srv = serve(0, 32)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/v1/embeddings",
                                     json.dumps({"model": "m", "input": ["a b", "c"]}).encode(),
                                     {"Content-Type": "application/json"})
        out = json.load(urllib.request.urlopen(req))
        assert [d["index"] for d in out["data"]] == [0, 1] and len(out["data"][0]["embedding"]) == 32
    finally:
        srv.shutdown()


class FakeMCP(BaseHTTPRequestHandler):
    """Streamable HTTP server with SSE replies; `ask` cites one real and one invented id."""
    tools = ["search_graph", "ask"]

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "id" not in msg:
            self.send_response(202)
            self.end_headers()
            return
        if msg["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "f", "version": "0"}}
        elif msg["method"] == "tools/list":
            result = {"tools": [{"name": n, "inputSchema": {"type": "object"}} for n in self.tools]}
        else:
            q = msg["params"]["arguments"]["question"]
            result = {"content": [], "structuredContent": {
                "answer": f"A refund needs the order to be cancelled first. ({q})",
                "citations": [{"id": "bookshop:api/orders.py#refund_order", "confidence": 0.9},
                              {"id": "bookshop:api/made_up.py#nothing", "confidence": 0.2}]}}
        body = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result})}\n\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Mcp-Session-Id", "s1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


class FakeWriter:
    """Answers the existence query of eval_answer.existing_ids."""
    def __init__(self, ids):
        self.ids = set(ids)

    def _run(self, query, ids):
        class R:
            records = [{"id": i} for i in ids if i in self.ids]
        return R()


@pytest.fixture
def fake_mcp():
    def start(tools):
        handler = type("H", (FakeMCP,), {"tools": tools})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_port}/mcp"
    servers = []
    yield start
    for s in servers:
        s.shutdown()


def test_mcp_client_and_answer_eval(fake_mcp):
    url = fake_mcp(["search_graph", "ask"])
    with MCPClient(url) as c:
        assert c.tool_names() == {"search_graph", "ask"}
    cases = [{"question": "What does a refund need?", "expect": ["refund_order", "cancel_order"], "facts": [["cancel", "cancelled"], "tax"]}]
    res = eval_answers(FakeWriter({"bookshop:api/orders.py#refund_order"}), "bookshop", cases, url)
    assert res["cases"] == 1 and "skipped" not in res
    row = res["rows"][0]
    assert row["citation_recall"] == 0.5 and row["citation_validity"] == 0.5 and row["fact_coverage"] == 0.5
    assert row["missing_citations"] == ["cancel_order"] and row["invalid_citations"] == ["bookshop:api/made_up.py#nothing"]


def test_answer_eval_skips_without_ask_tool(fake_mcp):
    cases = [{"question": "q", "expect": ["x"]}]
    assert "no ask tool" in eval_answers(None, "r", cases, fake_mcp(["search_graph"]))["skipped"]
    assert eval_answers(None, "r", cases, None)["skipped"] if not os.environ.get("KG_MCP_URL") else True
    assert "not reachable" in eval_answers(None, "r", cases, "http://127.0.0.1:9/mcp")["skipped"]
    assert eval_answers(None, "r", [], "http://x")["cases"] == 0


def test_citation_matching_and_facts():
    assert split_id("precept:backend/app/routers/orgs.py#create_invitation") == ("create_invitation", "backend/app/routers/orgs.py")
    assert split_id("precept:backend/app/security/crypto.py") == ("", "backend/app/security/crypto.py")
    assert cited("create_invitation", {"id": "precept:backend/app/routers/orgs.py#create_invitation"})
    assert cited("crypto.py", {"id": "precept:backend/app/security/crypto.py#EncryptedSecret"})
    assert cited("jira:PREC-12", {"id": "jira:PREC-12"})
    assert not cited("login", {"id": "precept:backend/app/routers/auth.py#logout"})
    assert facts_by_string("It returns HTTP 409 Conflict.\nThe  invite stays pending.", [[409, "duplicate"], "pending", "owner"]) == [True, True, False]
    assert score_case({"question": "q", "expect": []}, "", [], set(), [])["citation_validity"] == 0.0


def test_gates():
    res = {"search": {"hit_at_k": 0.9, "mrr": 0.4}, "impact": {"cases": 2, "recall": 1.0},
           "answer": {"cases": 3, "citation_recall": 0.5, "citation_validity": 1.0, "fact_coverage": 0.9}}
    assert gate_failures(res, {"min_hit": 0.8, "min_mrr": 0.5, "min_citation_recall": 0.6}) == ["mrr 0.4 < 0.5", "citation_recall 0.5 < 0.6"]
    res["answer"] = {"cases": 3, "skipped": "no ask tool"}
    assert gate_failures(res, {"min_citation_recall": 0.6, "min_impact": 1.0}) == []


def test_sse_parser():
    body = 'event: message\r\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\r\n\r\n: ping\n\nid: 3\ndata: not json\n\n'
    assert _sse_messages(body) == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_backup_values_schema_and_files(tmp_path):
    from neo4j.time import DateTime

    dt = DateTime(2026, 11, 4, 2, 30, 0, tzinfo=None)
    assert backup.decode(backup.encode([dt, 1, "x"])) == [dt, 1, "x"]
    assert backup.idempotent("CREATE CONSTRAINT `code_id` FOR (n:`Code`) REQUIRE (n.`id`) IS UNIQUE") == \
        "CREATE CONSTRAINT `code_id` IF NOT EXISTS FOR (n:`Code`) REQUIRE (n.`id`) IS UNIQUE"
    good, bad = tmp_path / "neo4j-20261104T000000Z.jsonl.gz", tmp_path / "neo4j-20261103T000000Z.jsonl.gz"
    lines = [{"format": "kg-backup", "version": 1, "created": "now", "schema": []},
             {"n": "1", "l": ["A"], "p": {}}, {"r": "R", "s": "1", "e": "1", "p": {}}]
    with gzip.open(good, "wt") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines + [{"end": True, "nodes": 1, "relationships": 1}]) + "\n")
    with gzip.open(bad, "wt") as fh:  # no end line: an interrupted dump
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    assert backup.check(good)["relationships"] == 1
    with pytest.raises(ValueError, match="incomplete"):
        backup.check(bad)
    assert backup.prune(tmp_path, 1) == [bad.name] and good.exists()


def test_tracing_is_off_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.setattr(telemetry, "_state", {})
    with telemetry.span("x") as s:
        assert s is None

    def asset(context):
        return 1
    assert list(inspect.signature(telemetry.traced_asset(asset)).parameters) == ["context"]  # Dagster reads it


def test_llm_and_embedding_spans(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from kg_pipeline.models import LLM, Embedder

    exporter, provider = InMemorySpanExporter(), TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_state", {"tracer": provider.get_tracer("t")})
    monkeypatch.setattr("kg_pipeline.models._post", lambda url, body, headers=None, **kw: (
        {"message": {"content": '{"ok": true}'}} if "chat" in url else {"data": [{"index": 0, "embedding": [0.1]}]}))
    assert LLM().json("system", "user", {"type": "object"}) == {"ok": True}
    assert Embedder().query("q") == [0.1]
    spans = {s.name: s for s in exporter.get_finished_spans()}
    llm = next(s for n, s in spans.items() if n.startswith("chat "))
    assert llm.attributes["openinference.span.kind"] == "LLM" and "input.value" not in llm.attributes
    assert any(n.startswith("embeddings ") for n in spans)


def test_scale_generator_shapes():
    g, syms = build_repo("scale-r000", 3, 12, random.Random(1))
    g2, _ = build_repo("scale-r000", 3, 12, random.Random(1))
    assert sorted(g.edges) == sorted(g2.edges)  # deterministic
    assert all(e.src in g.nodes and e.dst in g.nodes for e in g.edges.values())
    assert any(n.props.get("path", "").startswith("tests/") for n in g.nodes.values())
    _, rows, edges = tickets(120, ["SCA"], syms, random.Random(1))
    keys = {r["id"] for r in rows}
    assert len(rows) == 120 and all(e["dst"] in keys for e in edges["CHILD_OF"] + edges["LINKS"])


@NEEDS_NEO4J
def test_backup_roundtrip_and_scale_into_neo4j(tmp_path):
    from kg_pipeline.graph.neo4j_writer import Neo4jWriter
    from scale.generate import generate

    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), "neo4j", os.environ["NEO4J_PASSWORD"]) as w:
        cleanup = ("MATCH (n) WHERE n.repo = 'kgt-scale-r000' OR n.id STARTS WITH 'jira:KGSA' "
                   "OR n.id STARTS WITH 'jira:release:KGSA' OR n.id STARTS WITH 'docs:kgt-scale:' DETACH DELETE n")
        w._run(cleanup)
        try:
            stats = generate(w, 1, 2, 6, 60, 2, 3, 12, dimensions=0, prefix="kgt-scale", project="KGS")  # no vectors: the index size belongs to the database
            assert stats["tickets"] == 60 and stats["sections"] == 6 and stats["commits"] == 12
            calls = w._run("MATCH (:Symbol {repo: 'kgt-scale-r000'})-[c:CALLS]->() RETURN count(c) AS n").records[0]["n"]
            refs = w._run("MATCH (:Commit {repo: 'kgt-scale-r000'})-[r:REFERENCES]->(:Ticket) RETURN count(r) AS n").records[0]["n"]
            assert calls > 0 and refs == 12

            out = backup.dump(w, tmp_path / "neo4j-x.jsonl.gz")
            assert backup.check(Path(out["file"]))["nodes"] == out["nodes"]
            # Restore only the generated repository's code into the cleaned database.
            header, lines = backup.read(Path(out["file"]))
            mine, keep = set(), []
            for row in lines:
                if "n" in row and row["p"].get("repo") == "kgt-scale-r000" and "Code" in row["l"]:
                    mine.add(row["n"])
                    keep.append(row)
                elif "r" in row and row["s"] in mine and row["e"] in mine:
                    keep.append(row)
            part = tmp_path / "neo4j-part.jsonl.gz"
            with gzip.open(part, "wt", encoding="utf-8") as fh:
                rels = sum("r" in r for r in keep)
                for row in [header] + keep + [{"end": True, "nodes": len(keep) - rels, "relationships": rels}]:
                    fh.write(json.dumps(row) + "\n")
            before = w._run("MATCH (n:Code {repo: 'kgt-scale-r000'}) RETURN count(n) AS n").records[0]["n"]
            w._run("MATCH (n:Code {repo: 'kgt-scale-r000'}) DETACH DELETE n")
            res = backup.restore(w, part, force=True)
            after = w._run("MATCH (n:Code {repo: 'kgt-scale-r000'}) RETURN count(n) AS n").records[0]["n"]
            again = w._run("MATCH (:Symbol {repo: 'kgt-scale-r000'})-[c:CALLS]->() RETURN count(c) AS n").records[0]["n"]
            assert res["nodes"] == before == after and again == calls
            assert not w._run(f"MATCH (n:{backup.TEMP_LABEL}) RETURN n LIMIT 1").records
        finally:
            w._run(cleanup)
