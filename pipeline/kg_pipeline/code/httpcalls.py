"""HTTP routes, HTTP client calls and message topics found in code (regular expressions, no LLM).

Per repository this module writes facts on Symbol nodes; links.py turns them into edges across repos:

    endpoint_method, endpoint_route   full route of a server endpoint (router and controller prefixes applied)
    http_calls      ["POST /api/orders @ORDERS_URL", ...]  client or test-client calls (@hint = base-URL name or host)
    publishes, subscribes  ["kafka:order-created", ...]  message topics with literal names

Best effort: a call is found only when its URL or path is a literal or a template that starts with a
literal path or a base-URL placeholder. Dynamic URLs are skipped and counted.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from ..domain.roles import classify
from .files import SourceFile
from .model import CodeGraph
from .treesitter import FileSyntax

VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_V = r"(get|post|put|patch|delete|head|options)"
# A quoted URL, optionally after "BASE +" (Java, JS, Python concatenation) or "f" (Python f-string).
_URL = r"""(?:(?P<pre>[A-Za-z_][\w.]*)\s*\+\s*)?f?(?P<q>["'`])(?P<url>[^"'`\n]{1,300})(?P=q)(?P<cat>\s*\+)?"""

CLIENT = [
    # axios.get, client.post (TestClient, supertest), requests.get, httpx.post, restTemplate.getForObject,
    # MockMvc get("/x"), C# GetAsync / PostAsJsonAsync, Go http.Get
    re.compile(rf"(?i)(?<![\w$])(?:[\w$]+\s*\.\s*)?{_V}(?:ForObject|ForEntity|ForLocation|AsJsonAsync|Async|Json)?\s*\(\s*{_URL}"),
    re.compile(rf"(?i)\.(?:request|fetch)\s*\(\s*[\"'](?P<verb>\w+)[\"']\s*,\s*{_URL}"),             # requests.request("GET", u)
    re.compile(rf"(?:http|httptest)\.NewRequest(?:WithContext)?\s*\(\s*(?:\w+\s*,\s*)?(?:http\.Method(?P<verb>\w+)|\"(?P<verb2>\w+)\")\s*,\s*{_URL}"),
    re.compile(rf"\bexchange\s*\(\s*{_URL}\s*,\s*HttpMethod\.(?P<verb>\w+)"),                      # RestTemplate
    re.compile(rf"(?i)\.(?:method\s*\(\s*HttpMethod\.(?P<verb>\w+)\s*\)|{_V}\s*\(\s*\))\s*\.uri\s*\(\s*{_URL}"),  # WebClient
    re.compile(rf"new\s+HttpRequestMessage\s*\(\s*HttpMethod\.(?P<verb>\w+)\s*,\s*(?:new\s+Uri\s*\(\s*)?{_URL}"),   # C#
]
FETCH = re.compile(rf"(?<![\w$.])(?:fetch|ky|\$fetch|useFetch)\s*\(\s*{_URL}")
FETCH_METHOD = re.compile(r"""method\s*:\s*["'](\w+)["']""", re.I)

# Server-side route registrations without decorators: Express/Koa/Fastify, gin/echo/chi/net/http, ASP.NET minimal APIs.
ROUTE_RECEIVER = re.compile(r"^(app|router|server|r|e|g|mux|routes|v\d|group|\w*(Router|router|Group|group|Mux|mux))$")
REGISTER = re.compile(
    r"\b(?P<recv>[A-Za-z_$][\w$]*)\s*\.\s*(?P<fn>get|post|put|patch|delete|head|options|all|GET|POST|PUT|PATCH|DELETE|HEAD|"
    r"OPTIONS|Get|Post|Put|Patch|Delete|Head|Options|HandleFunc|Handle|MapGet|MapPost|MapPut|MapPatch|MapDelete)"
    r"\s*\(\s*(?P<q>[\"'`])(?P<path>(?:[A-Z]+ )?/[^\"'`\n]*)(?P=q)\s*,\s*(?P<rest>[^\n]*)")
GO_METHODS = re.compile(r"""\.Methods\(\s*"(\w+)"|^\s*"?(GET|POST|PUT|PATCH|DELETE) """)

# Topics: (pattern, broker, direction). The topic name is the group "name".
_N = r"""["'](?P<name>[\w.\-:/*>#]{1,200})["']"""
TOPICS = [
    (re.compile(rf"(?i)kafka\w*\.send\s*\(\s*{_N}"), "kafka", "publishes"),                          # kafkaTemplate.send
    (re.compile(rf"new\s+ProducerRecord\s*<[^>]*>\s*\(\s*{_N}"), "kafka", "publishes"),
    (re.compile(rf"(?i)producer\w*\.(?:send|produce|send_and_wait)\s*\(\s*{_N}"), "kafka", "publishes"),
    (re.compile(rf"(?i)producer\w*\.send\s*\(\s*\{{[^}}]*?topic\s*:\s*{_N}", re.S), "kafka", "publishes"),  # kafkajs
    (re.compile(rf"(?i)consumer\w*\.subscribe\s*\(\s*\{{[^}}]*?topics?\s*:\s*\[?\s*{_N}", re.S), "kafka", "subscribes"),
    (re.compile(rf"(?i)consumer\w*\.subscribe\s*\(\s*(?:\[|List\.of\(|Arrays\.asList\(|Collections\.singletonList\()\s*{_N}"), "kafka", "subscribes"),
    (re.compile(rf"KafkaConsumer\s*\(\s*{_N}"), "kafka", "subscribes"),
    (re.compile(rf"(?i)basic_publish\s*\([^)]*?routing_key\s*=\s*{_N}", re.S), "rabbitmq", "publishes"),
    (re.compile(rf"(?i)basic_consume\s*\([^)]*?queue\s*=\s*{_N}", re.S), "rabbitmq", "subscribes"),
    (re.compile(rf"(?i)(?:rabbit|amqp)\w*\.convertAndSend\s*\(\s*{_N}"), "rabbitmq", "publishes"),
    (re.compile(rf"\.sendToQueue\s*\(\s*{_N}"), "rabbitmq", "publishes"),
    (re.compile(rf"(?i)channel\w*\.publish\s*\(\s*[\"'][^\"']*[\"']\s*,\s*{_N}"), "rabbitmq", "publishes"),
    (re.compile(rf"(?i)channel\w*\.consume\s*\(\s*{_N}"), "rabbitmq", "subscribes"),
    (re.compile(rf"(?i)send_message\s*\([^)]*?QueueUrl\s*=\s*{_N}", re.S), "sqs", "publishes"),
    (re.compile(rf"(?i)receive_message\s*\([^)]*?QueueUrl\s*=\s*{_N}", re.S), "sqs", "subscribes"),
    (re.compile(rf"(?i)Send(?:Message)?Command\s*\(\s*\{{[^}}]*?QueueUrl\s*:\s*{_N}", re.S), "sqs", "publishes"),
    (re.compile(rf"(?i)ReceiveMessageCommand\s*\(\s*\{{[^}}]*?QueueUrl\s*:\s*{_N}", re.S), "sqs", "subscribes"),
    (re.compile(rf"(?i)\b(?:nc|nats|js|jetstream|natsConn|conn)\w*\.(?:publish|request)\s*\(\s*{_N}"), "nats", "publishes"),
    (re.compile(rf"(?i)\b(?:nc|nats|js|jetstream|natsConn|conn)\w*\.(?:subscribe|queue_subscribe|QueueSubscribe)\s*\(\s*{_N}"), "nats", "subscribes"),
]
LISTENERS = [  # annotations / decorators on the subscribing symbol
    (re.compile(rf"^KafkaListener\b.*?(?:topics\s*=\s*\{{?\s*)?{_N}"), "kafka"),
    (re.compile(rf"^RabbitListener\b.*?(?:queues\s*=\s*\{{?\s*)?{_N}"), "rabbitmq"),
    (re.compile(rf"^SqsListener\b.*?{_N}"), "sqs"),
    (re.compile(rf"^(?:EventPattern|MessagePattern)\s*\(\s*{_N}"), "events"),
]

_PARAM = re.compile(r"\$\{[^}]*\}|\{[^}]*\}|<[^>]*>|(?<=/):[A-Za-z_]\w*|\[[^\]]+\]|%[sdv]|\*\*?")
_LEAD = re.compile(r"^(?:\$\{\s*([\w.]+)[^}]*\}|\{\s*([\w.]+)[^}]*\}|%s)")
_CONST = re.compile(r"""^\s*(?:export\s+)?(?:const\s+|let\s+|var\s+|final\s+|static\s+|private\s+|public\s+)*"""
                    r"""([A-Za-z_]\w*)\s*(?::\s*[\w.]+)?\s*=\s*f?["'`]([^"'`\n]+)["'`]\s*;?\s*$""", re.M)
_HOST = re.compile(r"^[a-z][a-z0-9+.-]*://([^/:?#]+)(?::\d+)?", re.I)


def norm_route(path: str) -> str:
    """'/api/Orders/{id}/' -> '/api/orders/{}'. Parameters of any style become {}."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    path = re.sub(r"(?<=[^/])\$\{[^}]*\}$", "", path)     # `/api/x${query}`: a query string, not a segment
    path = _PARAM.sub("{}", path)
    path = re.sub(r"/+", "/", "/" + path.strip("/")).lower()
    return path if path != "/" else "/"


def join(*parts: str | None) -> str:
    return "/" + "/".join(p.strip("/") for p in parts if p and p.strip("/"))


def split_url(url: str, pre: str | None = None) -> tuple[str, str | None] | None:
    """URL or path literal -> (path, hint). hint is a base-URL variable name or a host. None: not a URL."""
    url, hint = url.strip(), pre
    m = _HOST.match(url)
    if m:
        hint = m.group(1).lower()
        url = url[m.end():] or "/"
    else:
        lead = _LEAD.match(url)
        if lead:
            hint = (lead.group(1) or lead.group(2) or lead.group(0)).split(".")[-1]
            url = url[lead.end():]
    if not url.startswith("/") or " " in url.strip():
        return None
    return url, hint


# ---- endpoint routes ---------------------------------------------------------------------------

_PY_ROUTER = re.compile(r"^(\w+)\s*=\s*(?:\w+\.)?(APIRouter|Blueprint|Router)\s*\(([^\n]*)", re.M)
_PY_PREFIX = re.compile(r"""(?:prefix|url_prefix)\s*=\s*["']([^"']*)["']""")
_PY_INCLUDE = re.compile(r"""^\s*(\w+)\.(?:include_router|register_blueprint)\s*\(\s*([\w.]+)([^\n]*)""", re.M)
_PY_FROM = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+([^\n(]+|\([^)]*\))", re.M)
_ANN_STRING = re.compile(r"""["']([^"']*)["']""")
_ANN_SKIP = re.compile(r"""\b(produces|consumes|headers|params|name|Name|Order)\s*=\s*(\{[^}]*\}|"[^"]*"|'[^']*'|\w+)""")


def _ann_route(annotation: str) -> str | None:
    m = _ANN_STRING.search(_ANN_SKIP.sub("", annotation))
    return m.group(1) if m else None


def _python_prefixes(files: list[SourceFile]) -> dict[tuple[str, str], str]:
    """(file path, router variable) -> full prefix, following include_router / register_blueprint."""
    own: dict[tuple[str, str], str] = {}
    by_module: dict[str, str] = {}
    for f in files:
        if f.language != "python":
            continue
        mod = f.path[:-3].replace("/", ".")
        by_module[mod.removesuffix(".__init__")] = f.path
        for var, _, args in _PY_ROUTER.findall(f.text):
            p = _PY_PREFIX.search(args)
            own[(f.path, var)] = p.group(1) if p else ""
    parent: dict[tuple[str, str], tuple[tuple[str, str], str]] = {}
    for f in files:
        if f.language != "python" or ".include_router" not in f.text and ".register_blueprint" not in f.text:
            continue
        imported: dict[str, str] = {}     # local name -> dotted module or module.attr
        for module, names in _PY_FROM.findall(f.text):
            for n in re.split(r"[,\s()]+", names):
                if n and n != "as":
                    imported[n] = f"{module}.{n}"
        for pvar, child, rest in _PY_INCLUDE.findall(f.text):
            extra = _PY_PREFIX.search(rest)
            head, _, attr = child.rpartition(".")
            if head:   # module.router
                target_mod, var = imported.get(head, head), attr
            else:      # imported router variable
                target_mod, _, var = imported.get(child, child).rpartition(".")
            target_mod = target_mod.lstrip(".")
            path = next((p for m, p in by_module.items() if m == target_mod or m.endswith("." + target_mod)), None)
            if path and (path, var) in own:
                parent[(path, var)] = ((f.path, pvar), extra.group(1) if extra else "")
    full: dict[tuple[str, str], str] = {}
    for key in own:
        parts, cur, seen = [own[key]], key, {key}
        while cur in parent:
            (cur, extra) = parent[cur]
            if cur in seen:
                break
            seen.add(cur)
            parts = [own.get(cur, ""), extra] + parts
        full[key] = join(*parts)
    return full


def endpoint_routes(g: CodeGraph, files: list[SourceFile], syntax: dict[str, FileSyntax]) -> None:
    """Set endpoint_method / endpoint_route on endpoint symbols; add Route symbols for registrations."""
    prefixes = _python_prefixes(files)
    for f in files:
        s = syntax.get(f.path)
        if s is None:
            continue
        by_qual = {sym.qualname: sym for sym in s.symbols}
        for sym in s.symbols:
            r = classify(sym.kind, sym.annotations, sym.supertypes)
            if not r or r["role"] != "endpoint":
                continue
            ann = next(a for a in sym.annotations if classify(sym.kind, [a], None))
            if ann.startswith("api_view"):   # Django REST: the route is in urls.py
                continue
            verb = r["http_method"]
            if not verb:
                vm = re.search(r"RequestMethod\.(\w+)|methods\s*=\s*\[\s*[\"'](\w+)", ann)
                verb = (vm.group(1) or vm.group(2)).upper() if vm else None
            route = _ann_route(ann) or ""
            prefix = ""
            if f.language == "python":
                var = ann.split(".", 1)[0]
                prefix = prefixes.get((f.path, var), "")
            elif sym.parent in by_qual:
                owner = by_qual[sym.parent]
                for a in owner.annotations:
                    if re.match(r"(RequestMapping|Controller|Route)\b", a) and _ann_route(a):
                        prefix = _ann_route(a).replace("[controller]", owner.name.removesuffix("Controller"))
                        break
            sid = f"{g.repo}:{f.path}#{sym.qualname}"
            if sid in g.nodes:
                g.nodes[sid].props.update(endpoint_method=verb or "ANY", endpoint_route=join(prefix, route))
                g.counters["http_endpoints"] += 1
        _registrations(g, f, s)


def _is_registration(m: re.Match) -> bool:
    recv, fn = m.group("recv"), m.group("fn")
    if fn in {"HandleFunc", "Handle"}:
        return True
    return bool(ROUTE_RECEIVER.match(recv)) and recv not in {"http", "axios", "client", "request"}


def _registrations(g: CodeGraph, f: SourceFile, s: FileSyntax) -> None:
    if f.language not in {"javascript", "typescript", "tsx", "go", "csharp"}:
        return
    names = {sym.name: sym for sym in s.symbols}
    for m in REGISTER.finditer(f.text):
        if not _is_registration(m):
            continue
        fn, path, rest = m.group("fn"), m.group("path"), m.group("rest")
        verb = fn.upper().removeprefix("MAP")
        if fn in {"HandleFunc", "Handle"}:
            gm = GO_METHODS.search(rest) or re.match(r"(GET|POST|PUT|PATCH|DELETE) ", path)
            verb = next((x for x in (gm.groups() if gm else ()) if x), "ANY")
        path = path.split(" ", 1)[-1]
        if verb == "ALL":
            verb = "ANY"
        line = f.text.count("\n", 0, m.start()) + 1
        # The handler is the last name in the argument list defined in this file (middleware comes first).
        handler = next((names[h.split(".")[-1]] for h in reversed(re.findall(r"[A-Za-z_$][\w$.]*", rest))
                        if h.split(".")[-1] in names), None)
        if handler is not None:
            sid = f"{g.repo}:{f.path}#{handler.qualname}"
        else:   # inline handler: a Route symbol at the registration line
            sid = f"{g.repo}:{f.path}#route:{verb} {path}"
            g.add_node(sid, ("Code", "Symbol", "Route"), name=f"{verb} {path}", qualname=f"route:{verb} {path}",
                       kind="Route", path=f.path, language=f.language, start_line=line, end_line=line,
                       signature=f.text.splitlines()[line - 1].strip()[:300])
            g.add_edge("DEFINES", f"{g.repo}:{f.path}", sid, "parser")
        if sid in g.nodes:
            g.nodes[sid].props.update(endpoint_method=verb, endpoint_route=join(path))
            g.counters["http_endpoints"] += 1


# ---- client calls and topics -------------------------------------------------------------------

def _enclosing(s: FileSyntax, line: int):
    best = None
    for sym in s.symbols:
        if sym.start_line <= line <= sym.end_line and (best is None or sym.end_line - sym.start_line < best.end_line - best.start_line):
            best = sym
    return best


def client_calls(text: str) -> list[tuple[int, str, str, str | None]]:
    """(offset, METHOD, path, hint) for each HTTP client call with a literal or template URL in text."""
    found: dict[int, tuple[int, str, str, str | None]] = {}
    registered = {m.start("q") for m in REGISTER.finditer(text) if _is_registration(m)}
    for pattern in CLIENT:
        for m in pattern.finditer(text):
            if m.start("q") in registered or m.start("q") in found:
                continue
            if text[text.rfind("\n", 0, m.start()) + 1:m.start()].lstrip().startswith(("@", "[")):
                continue   # a decorator or attribute declares a route; it does not call one
            gd = m.groupdict()
            verb = next((x.upper() for x in (gd.get("verb"), gd.get("verb2"), *m.groups()) if x and x.upper() in VERBS), None)
            if verb is None:
                continue
            parts = split_url(m.group("url"), gd.get("pre"))
            if parts:
                path = parts[0] + ("{}" if gd.get("cat") else "")
                found[m.start("q")] = (m.start(), verb, path, parts[1])
    for m in FETCH.finditer(text):
        parts = split_url(m.group("url"), m.group("pre"))
        if parts and m.start("q") not in found:
            tail, depth = text[m.end():m.end() + 400], 1     # the rest of this fetch(...) call only
            for i, ch in enumerate(tail):
                depth += {"(": 1, ")": -1}.get(ch, 0)
                if depth == 0:
                    tail = tail[:i]
                    break
            verb = FETCH_METHOD.search(tail)
            found[m.start("q")] = (m.start(), verb.group(1).upper() if verb else "GET",
                                   parts[0] + ("{}" if m.group("cat") else ""), parts[1])
    return sorted(found.values())


def topics(text: str) -> list[tuple[int, str, str, str]]:
    """(offset, direction, broker, name) for message publish/subscribe calls with literal names."""
    out = []
    for pattern, broker, direction in TOPICS:
        for m in pattern.finditer(text):
            name = m.group("name")
            if broker == "sqs":
                name = name.rstrip("/").rsplit("/", 1)[-1]
            out.append((m.start(), direction, broker, name))
    return sorted(set(out))


def annotate(g: CodeGraph, files: list[SourceFile], syntax: dict[str, FileSyntax]) -> None:
    """Write http_calls / publishes / subscribes on the innermost symbol around each call."""
    endpoint_routes(g, files, syntax)
    for f in files:
        s = syntax.get(f.path)
        if s is None:
            continue
        facts: dict[str, dict[str, set[str]]] = {}

        def add(line: int, key: str, value: str) -> None:
            sym = _enclosing(s, line)
            sid = f"{g.repo}:{f.path}#{sym.qualname}" if sym else f"{g.repo}:{f.path}"
            facts.setdefault(sid, {}).setdefault(key, set()).add(value)

        consts = {}       # NAME = "/api/x" or `${API_BASE}/api/x`: a base that already holds a path
        for name, value in _CONST.findall(f.text):
            parts = split_url(value)
            if parts:
                consts[name] = parts
        for offset, verb, path, hint in client_calls(f.text):
            if hint in consts:
                base, hint = consts[hint]
                path = base.rstrip("/") + path
            add(f.text.count("\n", 0, offset) + 1, "http_calls", f"{verb} {path}" + (f" @{hint}" if hint else ""))
            g.counters["http_calls"] += 1
        for offset, direction, broker, name in topics(f.text):
            add(f.text.count("\n", 0, offset) + 1, direction, f"{broker}:{name}")
            g.counters[f"topic_{direction}"] += 1
        for sym in s.symbols:
            for a in sym.annotations:
                for pattern, broker in LISTENERS:
                    m = pattern.search(a)
                    if m:
                        facts.setdefault(f"{g.repo}:{f.path}#{sym.qualname}", {}).setdefault(
                            "subscribes", set()).add(f"{broker}:{m.group('name')}")
                        g.counters["topic_subscribes"] += 1
        for sid, props in facts.items():
            if sid in g.nodes:
                g.nodes[sid].props.update({k: sorted(v) for k, v in props.items()})


def is_test_path(path: str) -> bool:
    """Same rule as testPathRegex in the MCP server (mcp/search.go)."""
    p = PurePosixPath(path)
    return bool(re.search(r"(^|/)(tests?|__tests__|spec)/", path, re.I)
                or re.search(r"(_test|_spec|\.test|\.spec|Test|Tests)\.[a-z]+$", p.name, re.I)
                or p.name.lower().startswith("test_"))
