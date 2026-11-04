import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from kg_pipeline import search
from kg_pipeline.rerank import BATCH, Reranker


class _TEI(BaseHTTPRequestHandler):
    """Fake TEI /rerank: the score is the text length; results sorted by score like TEI."""
    calls: list[dict] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _TEI.calls.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if self.path != "/rerank" or len(body["texts"]) > BATCH:
            self.send_response(413)
            self.end_headers()
            return
        res = sorted(({"index": i, "score": len(t) / 1000} for i, t in enumerate(body["texts"])), key=lambda r: -r["score"])
        data = json.dumps(res).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture()
def tei():
    srv = HTTPServer(("127.0.0.1", 0), _TEI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _TEI.calls = []
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


HITS = [(f"c{i:02d}", 1 / (i + 1)) for i in range(40)]
TEXTS = {cid: "x" * (i + 1) for i, (cid, _) in enumerate(HITS)}  # the last RRF hit has the longest text


def test_rerank_orders_by_cross_encoder_and_batches(tei):
    r = Reranker(url=tei + "/", key="k", pool=40)
    got = r.rerank("invite users", HITS, 5, lambda ids: {i: TEXTS[i] for i in ids})
    assert [g[0] for g in got] == ["c39", "c38", "c37", "c36", "c35"]
    assert got[0][1] == pytest.approx(0.04)
    assert len(_TEI.calls) == 2  # 40 texts in batches of 32
    assert _TEI.calls[0]["auth"] == "Bearer k" and _TEI.calls[0]["body"]["query"] == "invite users"


def test_rerank_pool_limits_candidates(tei):
    r = Reranker(url=tei, key="", pool=10)
    got = r.rerank("q", HITS, 3, lambda ids: {i: TEXTS[i] for i in ids})
    assert [g[0] for g in got] == ["c09", "c08", "c07"]  # only the top 10 RRF hits are reranked
    assert len(_TEI.calls[0]["body"]["texts"]) == 10


def test_rerank_falls_back_to_rrf_on_error(tei, capsys):
    r = Reranker(url=tei + "/missing", key="", pool=40)  # 413 from the fake server
    got = r.rerank("q", HITS, 3, lambda ids: {i: TEXTS[i] for i in ids})
    assert got == HITS[:3]
    assert "reranker off" in capsys.readouterr().err


def test_rerank_off_without_url(monkeypatch):
    monkeypatch.delenv("RERANK_URL", raising=False)
    r = Reranker()
    assert not r.enabled
    assert r.rerank("q", HITS, 4, lambda ids: pytest.fail("no text lookup when off")) == HITS[:4]


class _FakeWriter:
    """Answers the three ranking queries of search.candidates and the passage query."""

    class _Res:
        def __init__(self, records):
            self.records = records

    def _run(self, cypher, **p):
        if "chunk_embedding" in cypher:
            return self._Res([{"id": "a"}, {"id": "b"}, {"id": "c"}])
        if "chunk_text" in cypher:
            return self._Res([{"id": "a"}, {"id": "c"}])
        if "concept_embedding" in cypher:
            return self._Res([])
        if "UNWIND $ids" in cypher:
            assert p["chars"] == 2000
            return self._Res([{"id": i, "text": {"a": "x", "b": "xxx", "c": "xx"}[i]} for i in p["ids"]])
        raise AssertionError(cypher)


def test_candidates_rerank_after_rrf(tei):
    w = _FakeWriter()
    plain = search.candidates(w, None, "r", "q", k=3, vector=[0.0], reranker=Reranker(url=""))
    assert [h[0] for h in plain] == ["a", "c", "b"]  # RRF order
    ranked = search.candidates(w, None, "r", "q", k=2, vector=[0.0], reranker=Reranker(url=tei))
    assert [h[0] for h in ranked] == ["b", "c"]
