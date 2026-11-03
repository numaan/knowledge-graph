"""Clients for the LLM (structured JSON output) and the embedding server.

LLM:        LLM_PROVIDER=ollama|openai, LLM_BASE_URL, LLM_MODEL, LLM_API_KEY (openai-compatible only)
Embeddings: EMBEDDINGS_URL (OpenAI-compatible /v1/embeddings, e.g. TEI), EMBEDDINGS_MODEL, EMBEDDINGS_API_KEY
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .telemetry import traced_embed, traced_llm

# Qwen3-Embedding is trained with an instruction on the query side only.
QUERY_INSTRUCTION = "Instruct: Given a question about a software system, retrieve relevant code and domain concepts\nQuery: "


def _post(url: str, body: dict, headers: dict | None = None, timeout: int = 900, retries: int = 3) -> dict:
    data = json.dumps(body).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data, {"Content-Type": "application/json", **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise RuntimeError(f"POST {url} failed: {e}") from e
            time.sleep(2 ** attempt * 2)
    raise AssertionError("unreachable")


class LLM:
    def __init__(self):
        self.provider = os.environ.get("LLM_PROVIDER", "ollama")
        self.base = os.environ.get("LLM_BASE_URL", "http://localhost:11434").rstrip("/")
        self.model = os.environ.get("LLM_MODEL", "qwen3.6:35b-a3b")
        self.key = os.environ.get("LLM_API_KEY", "")
        self.context = int(os.environ.get("LLM_CONTEXT_TOKENS", "16384"))

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.model}"

    @traced_llm
    def json(self, system: str, user: str, schema: dict) -> dict:
        """One chat call that must return JSON matching schema."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if self.provider == "ollama":
            r = _post(f"{self.base}/api/chat", {
                "model": self.model, "messages": messages, "stream": False, "think": False,
                "format": schema, "options": {"temperature": 0, "num_ctx": self.context}})
            text = r["message"]["content"]
        else:
            r = _post(f"{self.base}/v1/chat/completions", {
                "model": self.model, "messages": messages, "temperature": 0,
                "response_format": {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}}},
                {"Authorization": f"Bearer {self.key}"} if self.key else None)
            text = r["choices"][0]["message"]["content"]
        return json.loads(text)


class Embedder:
    def __init__(self):
        self.url = os.environ.get("EMBEDDINGS_URL", "http://localhost:8080").rstrip("/")
        self.model = os.environ.get("EMBEDDINGS_MODEL", "Qwen/Qwen3-Embedding-0.6B")
        self.dimensions = int(os.environ.get("EMBEDDINGS_DIMENSIONS", "1024"))
        self.key = os.environ.get("EMBEDDINGS_API_KEY", "")
        self.batch = int(os.environ.get("EMBEDDINGS_BATCH", "16"))

    @traced_embed
    def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            r = _post(f"{self.url}/v1/embeddings", {"model": self.model, "input": texts[i:i + self.batch]},
                      {"Authorization": f"Bearer {self.key}"} if self.key else None)
            out += [d["embedding"] for d in sorted(r["data"], key=lambda d: d["index"])]
        return out

    def documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def query(self, text: str) -> list[float]:
        return self._embed([QUERY_INSTRUCTION + text])[0]
