"""Deterministic embedding server for CI, scale tests and offline work (no model, no GPU).

    kg-fake-embedder --port 8089 --dimensions 256
    EMBEDDINGS_URL=http://localhost:8089 EMBEDDINGS_DIMENSIONS=256 kg-embed --repo my-repo

Serves the OpenAI-compatible POST /v1/embeddings that the pipeline and the MCP server call. Each text
becomes a hashed bag of words (camelCase and snake_case split, plural s removed), L2-normalised, so
texts that share words are close. The same text always gives the same vector, on every machine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_WORD = re.compile(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])|[0-9]+")
_STOP = {"the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "is", "it", "by", "with", "at", "be",
         "how", "what", "which", "who", "does", "do", "can", "this", "that", "from", "as", "are"}


def tokens(text: str) -> list[str]:
    # The pipeline and the MCP server put an instruction in front of queries; it carries no meaning here.
    if text.startswith("Instruct:") and "\nQuery: " in text:
        text = text.split("\nQuery: ", 1)[1]
    out = []
    for w in _WORD.findall(text):
        w = w.lower()
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if w not in _STOP and len(w) > 1:
            out.append(w)
    return out


def vector(text: str, dimensions: int) -> list[float]:
    v = [0.0] * dimensions
    counts: dict[str, int] = {}
    for t in tokens(text):
        counts[t] = counts.get(t, 0) + 1
    for t, n in counts.items():
        h = hashlib.blake2b(t.encode(), digest_size=8).digest()
        i = int.from_bytes(h[:4], "little") % dimensions
        v[i] += (1.0 + math.log(n)) * (1 if h[4] & 1 else -1)
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0:  # empty text: a fixed unit vector (cosine similarity needs a non-zero vector)
        v[0], norm = 1.0, 1.0
    return [x / norm for x in v]


def handler(dimensions: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._send(200, {"status": "ok", "dimensions": dimensions})

        def do_POST(self):
            if self.path.rstrip("/") != "/v1/embeddings":
                return self._send(404, {"error": "not found"})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            texts = body.get("input", [])
            texts = [texts] if isinstance(texts, str) else texts
            self._send(200, {"object": "list", "model": body.get("model", "fake"),
                             "data": [{"object": "embedding", "index": i, "embedding": vector(t, dimensions)}
                                      for i, t in enumerate(texts)]})

        def _send(self, code: int, payload: dict):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # quiet: one line per request is noise in CI logs
            pass

    return Handler


def serve(port: int, dimensions: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), handler(dimensions))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8089)
    ap.add_argument("--dimensions", type=int, default=256)
    args = ap.parse_args(argv)
    srv = serve(args.port, args.dimensions, args.host)
    print(f"fake embedder on http://{args.host}:{args.port}/v1/embeddings ({args.dimensions} dimensions)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
