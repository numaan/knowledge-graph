"""Minimal MCP client over Streamable HTTP (JSON-RPC), standard library only.

    with MCPClient("http://localhost:8000/mcp") as c:
        names = c.tool_names()
        result = c.call("search_graph", {"query": "who can invite users"})

Used by kg-eval (answer cases) and the agent benchmark. Reads both reply styles of the transport:
a plain JSON body and a text/event-stream body.
"""
from __future__ import annotations

import itertools
import json
import os
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, url: str, token: str | None = None, timeout: float = 120.0):
        self.url = url
        self.timeout = timeout
        self.session: str | None = None
        self.headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        token = token if token is not None else os.environ.get("KG_MCP_TOKEN", "")
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self._ids = itertools.count(1)

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *exc):
        self.close()

    def _post(self, message: dict) -> dict | None:
        headers = dict(self.headers)
        if self.session:
            headers["Mcp-Session-Id"] = self.session
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        req = urllib.request.Request(self.url, json.dumps(message).encode(), headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self.session = resp.headers.get("Mcp-Session-Id") or self.session
                body = resp.read().decode("utf-8", "replace")
                kind = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raise MCPError(f"HTTP {e.code} from {self.url}: {e.read()[:300]!r}") from e
        if "id" not in message or not body.strip():
            return None  # notification: 202 Accepted, no body
        replies = _sse_messages(body) if "text/event-stream" in kind else [json.loads(body)]
        for r in replies:
            if r.get("id") == message["id"]:
                if "error" in r:
                    raise MCPError(f"{message['method']}: {r['error'].get('message')}")
                return r.get("result", {})
        raise MCPError(f"no reply to {message['method']}")

    def request(self, method: str, params: dict | None = None) -> dict:
        return self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}})

    def initialize(self) -> dict:
        info = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                           "clientInfo": {"name": "kg-eval", "version": "0.1.0"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return info

    def tool_names(self) -> set[str]:
        names, cursor = set(), None
        while True:
            page = self.request("tools/list", {"cursor": cursor} if cursor else {})
            names |= {t["name"] for t in page.get("tools", [])}
            cursor = page.get("nextCursor")
            if not cursor:
                return names

    def call(self, name: str, arguments: dict) -> dict:
        """Structured result of a tool call; raises MCPError when the tool reports an error."""
        res = self.request("tools/call", {"name": name, "arguments": arguments})
        if res.get("isError"):
            text = " ".join(c.get("text", "") for c in res.get("content", []))
            raise MCPError(f"{name}: {text[:300]}")
        if res.get("structuredContent") is not None:
            return res["structuredContent"]
        for c in res.get("content", []):  # tools without an output schema return JSON as text
            if c.get("type") == "text":
                try:
                    return json.loads(c["text"])
                except ValueError:
                    return {"text": c["text"]}
        return {}

    def close(self) -> None:
        if not self.session:
            return
        req = urllib.request.Request(self.url, headers={**self.headers, "Mcp-Session-Id": self.session}, method="DELETE")
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except (urllib.error.URLError, TimeoutError):
            pass  # the server drops idle sessions itself
        self.session = None


def _sse_messages(body: str) -> list[dict]:
    out = []
    for event in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
        if data:
            try:
                out.append(json.loads(data))
            except ValueError:
                continue
    return out
