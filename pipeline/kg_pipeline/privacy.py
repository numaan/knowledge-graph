"""Secret and PII masking for text that leaves the source: graph writes, embeddings and LLM calls.

    KG_PII_MODE=mask (default) | off
    KG_PII_ALLOW=ci@example.org,re:^build-bot@     # extra allow-list: literal values or re:<regex>

Two classes of findings:
- secrets (private keys, cloud and SaaS tokens, JWTs, passwords in URLs and config lines) are never
  stored: redact() replaces them in the text that is written to the graph.
- PII (emails, phone numbers, card numbers, IBANs, US SSNs) stays in the stored text for readers with
  the ACL, but mask() replaces it, with the secrets, in text_masked, in embedding input and in LLM prompts.

Placeholders keep the kind, e.g. [SECRET:github_token] or [EMAIL], so that a model still sees that a
value was there. Values on the allow-list (example domains, documentation placeholders) are kept.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# (kind, pattern). The value to hide is group "v" when the pattern has one, else the whole match.
SECRETS: list[tuple[str, re.Pattern]] = [(k, re.compile(p)) for k, p in [
    ("private_key", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|\Z)"),
    ("aws_access_key", r"\b(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b"),
    ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{60,255})\b"),
    ("gitlab_token", r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    ("slack_token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    ("slack_webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}"),
    ("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("stripe_key", r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    ("atlassian_token", r"\bATATT3[A-Za-z0-9_=-]{20,}\b"),
    ("anthropic_key", r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    ("openai_key", r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b"),
    ("npm_token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("bearer_token", r"(?i)\b(?:authorization\s*[:=]\s*)?bearer\s+(?P<v>[A-Za-z0-9._~+/-]{20,}=*)"),
    ("url_password", r"\b[a-z][a-z0-9+.-]{1,20}://[^\s:/@]+:(?P<v>[^\s@/]{3,})@[^\s/]+"),
    # key = "value" in code and config; unquoted values only on a line of their own (.env, YAML, properties)
    ("password", r"(?im)\b[\w.-]*(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|"
                 r"client[_-]?secret|private[_-]?key)[\w.-]*[\"']?\s*[:=]\s*(?:[\"'](?P<v>[^\"'\s]{4,})[\"']"
                 r"|(?P<w>[^\s\"'(){}\[\],;]{6,})\s*$)"),
]]

PII: list[tuple[str, re.Pattern]] = [(k, re.compile(p)) for k, p in [
    ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
    ("card", r"\b(?=[2-6])(?:\d[ -]?){12,18}\d\b"),
    ("iban", r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b"),
    ("ssn", r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    # +44 20 7946 0958, (555) 123-4567, 555-123-4567, +1.555.123.4567; plain digit runs are not phones
    ("phone", r"(?<![\w.+-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?|\d{2,4}[ .-])\d{3,4}[ .-]\d{3,4}(?![\w-]|\.\d)"),
]]

SECRET_KINDS = {k for k, _ in SECRETS}

# Documentation placeholders and test domains are not real data.
ALLOW = [re.compile(p, re.I) for p in [
    r"@(?:[\w-]+\.)*(?:example\.(?:com|org|net)|example|test|invalid|localhost)$",
    r"^(?:noreply|no-reply|donotreply|git)@",
    r"^[<{$%]|[>}]$",                                       # <password>, ${TOKEN}, {{ secret }}, %PASS%
    r"^(?:x+|\*+|\.+|changeme|change_me|password|secret|redacted|dummy|example|placeholder|none|null|true|false)$",
    r"^\[(?:SECRET|EMAIL|PHONE|CARD|IBAN|SSN)",           # already masked
    r"^(?:os\.)?(?:environ|getenv|process\.env|System\.getenv)",
]]


@dataclass(frozen=True)
class Finding:
    kind: str
    category: str      # "secret" or "pii"
    start: int
    end: int


def mode() -> str:
    m = os.environ.get("KG_PII_MODE", "mask").strip().lower()
    if m not in ("mask", "off"):
        raise ValueError(f"KG_PII_MODE must be mask or off, not {m!r}")
    return m


def _allow_list() -> list[re.Pattern]:
    extra = []
    for item in filter(None, (s.strip() for s in os.environ.get("KG_PII_ALLOW", "").split(","))):
        extra.append(re.compile(item[3:]) if item.startswith("re:") else re.compile(f"^{re.escape(item)}$", re.I))
    return ALLOW + extra


def _luhn(digits: str) -> bool:
    total, odd = 0, False
    for ch in reversed(digits):
        d = int(ch) * (2 if odd else 1)
        total, odd = total + (d - 9 if d > 9 else d), not odd
    return total % 10 == 0


def _iban_ok(value: str) -> bool:
    s = value.replace(" ", "")
    n = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return 15 <= len(s) <= 34 and int(n) % 97 == 1


def scan(text: str, pii: bool = True) -> list[Finding]:
    """Non-overlapping findings, secrets first (a token inside a URL is one secret, not an email)."""
    if not text:
        return []
    allow = _allow_list()
    found: list[Finding] = []
    taken: list[tuple[int, int]] = []

    def add(kind: str, category: str, start: int, end: int) -> None:
        if any(start < e and s < end for s, e in taken):
            return
        taken.append((start, end))
        found.append(Finding(kind, category, start, end))

    for kind, rx in SECRETS + (PII if pii else []):
        category = "secret" if kind in SECRET_KINDS else "pii"
        for m in rx.finditer(text):
            group = next((g for g in ("v", "w") if g in rx.groupindex and m.group(g)), None)
            start, end = m.span(group) if group else m.span()
            value = text[start:end]
            if any(a.search(value) for a in allow):
                continue
            if kind == "card" and not _luhn(re.sub(r"\D", "", value)):
                continue
            if kind == "iban" and not _iban_ok(value):
                continue
            add(kind, category, start, end)
    return sorted(found, key=lambda f: f.start)


def _replace(text: str, findings: list[Finding]) -> str:
    out, pos = [], 0
    for f in findings:
        out += [text[pos:f.start], f"[SECRET:{f.kind}]" if f.category == "secret" else f"[{f.kind.upper()}]"]
        pos = f.end
    return "".join(out) + text[pos:]


def redact(text: str | None) -> str | None:
    """Text with secrets replaced: what may be stored. PII is kept."""
    if not text or mode() == "off":
        return text
    return _replace(text, scan(text, pii=False))


def mask(text: str | None) -> str | None:
    """Text with secrets and PII replaced: what may be embedded, sent to an LLM or shown to an agent."""
    if not text or mode() == "off":
        return text
    return _replace(text, scan(text))


def kinds(text: str | None) -> list[str]:
    return sorted({f.kind for f in scan(text or "")})


def protect_row(row: dict, fields: tuple[str, ...] = ("title", "text", "description")) -> dict:
    """A :Source row ready to store: secrets removed from fields, text_masked set when PII is in the text.

    pii lists the kinds found (for audits); detected secret values are not kept anywhere.
    """
    if mode() == "off":
        return row
    out = dict(row)
    found: set[str] = set()
    for f in fields:
        if isinstance(out.get(f), str):
            findings = scan(out[f])
            found |= {x.kind for x in findings}
            out[f] = _replace(out[f], [x for x in findings if x.category == "secret"])
            if f == "text" and any(x.category == "pii" for x in findings):
                out["text_masked"] = _replace(row[f], findings)
    if found:
        out["pii"] = sorted(found)
    return out
