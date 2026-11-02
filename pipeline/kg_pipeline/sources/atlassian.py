"""Shared client for Atlassian Cloud REST APIs (Jira and Confluence) and an ADF-to-Markdown converter."""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

Fetch = Callable[[str], dict]   # url -> JSON; tests pass a fake


class Atlassian:
    """Basic auth with an account email and API token (env), retries on 429/5xx with Retry-After."""

    def __init__(self, base_url: str | None = None, email: str | None = None, token: str | None = None,
                 fetch: Fetch | None = None, prefix: str = "JIRA"):
        self.base = (base_url or os.environ.get(f"{prefix}_BASE_URL", "")).rstrip("/")
        if not self.base:
            raise ValueError(f"{prefix}_BASE_URL is not set")
        email = email or os.environ.get(f"{prefix}_EMAIL") or os.environ.get("ATLASSIAN_EMAIL", "")
        token = token or os.environ.get(f"{prefix}_API_TOKEN") or os.environ.get("ATLASSIAN_API_TOKEN", "")
        self._auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode() if email and token else None
        self._fetch = fetch or self._http

    def _http(self, url: str, retries: int = 5) -> dict:
        headers = {"Accept": "application/json", **({"Authorization": self._auth} if self._auth else {})}
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                    raise RuntimeError(f"GET {url.split('?')[0]} failed: HTTP {e.code}") from e
                time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == retries - 1:
                    raise RuntimeError(f"GET {url.split('?')[0]} failed: {e}") from e
                time.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    def get(self, path: str, **params) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
        return self._fetch(f"{self.base}{path}{'?' + query if query else ''}")


def adf_to_markdown(node: dict | list | None) -> str:
    """Atlassian Document Format (Jira descriptions and comments) to plain Markdown."""
    if node is None:
        return ""
    if isinstance(node, list):
        return "".join(adf_to_markdown(n) for n in node)
    t, content, attrs = node.get("type"), node.get("content", []), node.get("attrs", {})
    inner = adf_to_markdown(content)
    if t == "text":
        text = node.get("text", "")
        marks = {m["type"] for m in node.get("marks", [])}
        return f"`{text}`" if "code" in marks else text
    if t in ("paragraph",):
        return inner + "\n\n"
    if t == "heading":
        return "#" * int(attrs.get("level", 2)) + " " + inner + "\n\n"
    if t == "hardBreak":
        return "\n"
    if t in ("bulletList", "orderedList"):
        return "".join(("- " if t == "bulletList" else f"{i}. ") + adf_to_markdown(li.get("content", [])).strip() + "\n"
                       for i, li in enumerate(content, 1)) + "\n"
    if t == "codeBlock":
        return f"```{attrs.get('language', '')}\n{inner}\n```\n\n"
    if t in ("mention", "emoji"):
        return attrs.get("text", "")
    if t in ("inlineCard", "blockCard"):
        return attrs.get("url", "")
    if t == "table":
        return "\n".join(" | ".join(adf_to_markdown(c.get("content", [])).strip() for c in row.get("content", []))
                         for row in content) + "\n\n"
    return inner
