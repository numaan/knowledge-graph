"""Cross-encoder reranker after RRF (Text Embeddings Inference /rerank API, BAAI/bge-reranker-v2-m3).

RERANK_URL (off when unset), RERANK_MODEL (informational: TEI serves one model), RERANK_API_KEY,
RERANK_CANDIDATES (how many RRF candidates to rerank, default 30).
"""
from __future__ import annotations

import os
import sys
from typing import Callable

from .models import _post

BATCH = 32         # TEI default --max-client-batch-size
PASSAGE_CHARS = 2000


class Reranker:
    def __init__(self, url: str | None = None, model: str | None = None, key: str | None = None,
                 pool: int | None = None):
        self.url = (url if url is not None else os.environ.get("RERANK_URL", "")).rstrip("/")
        self.model = model or os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
        self.key = key if key is not None else os.environ.get("RERANK_API_KEY", "")
        self.pool = max(1, pool or int(os.environ.get("RERANK_CANDIDATES", "30")))

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def scores(self, query: str, texts: list[str]) -> list[float]:
        """One relevance score per text, in the order of texts."""
        out = [0.0] * len(texts)
        for start in range(0, len(texts), BATCH):
            batch = [t[:PASSAGE_CHARS] for t in texts[start:start + BATCH]]
            res = _post(f"{self.url}/rerank", {"query": query, "texts": batch, "truncate": True, "raw_scores": False},
                        {"Authorization": f"Bearer {self.key}"} if self.key else None, timeout=60, retries=2)
            if len(res) != len(batch):
                raise RuntimeError(f"rerank: {len(res)} scores for {len(batch)} texts")
            for r in res:
                out[start + r["index"]] = float(r["score"])
        return out

    def rerank(self, query: str, hits: list[tuple[str, float]], k: int,
               text: Callable[[list[str]], dict[str, str]]) -> list[tuple[str, float]]:
        """Reorder the top RRF hits by cross-encoder score and keep k. On a reranker error, warn and
        keep the RRF order, so search works when the optional model server is down."""
        if not self.enabled or len(hits) < 2:
            return hits[:k]
        pool = hits[:max(self.pool, k)]
        try:
            texts = text([h[0] for h in pool])
            scores = self.scores(query, [texts.get(h[0], "") for h in pool])
        except Exception as e:  # noqa: BLE001 - any failure falls back to RRF
            print(f"warning: reranker off for this query: {e}", file=sys.stderr)
            return hits[:k]
        ranked = sorted(zip((h[0] for h in pool), scores), key=lambda x: -x[1])
        return ranked[:k]
