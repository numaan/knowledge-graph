"""OpenAPI 3.x (and Swagger 2.0) and AsyncAPI 2.x/3.x specs -> :Contract nodes.

    :Contract:ApiOperation   one per HTTP operation (method, path template, operationId, summary, schemas)
                             or AsyncAPI operation (method PUBLISH / SUBSCRIBE, channel)
    :Contract:Schema         one per named schema (components.schemas, definitions, components.messages)

Edges: File -DEFINES-> ApiOperation / Schema, ApiOperation -USES_SCHEMA-> Schema (role request or
response), Schema -USES_SCHEMA-> Schema ($ref). The links step (links.py) adds endpoint -IMPLEMENTS->
ApiOperation, callers -CALLS_HTTP-> and AsyncAPI ApiOperation -DESCRIBES-> Topic.

Specs are found by file name (openapi*, swagger*, asyncapi*, *.openapi.yaml) or by a top-level
openapi / swagger / asyncapi key in any YAML or JSON file.
"""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from urllib.parse import urlparse

import yaml

from .files import SourceFile
from .httpcalls import VERBS, join, norm_route
from .model import CodeGraph

TEXT_LIMIT = 4_000
_NAME = re.compile(r"(^|[._-])(openapi|swagger|asyncapi)([._-]|$)", re.I)
_KEY = re.compile(r"""^\s*["']?(openapi|swagger|asyncapi)["']?\s*:""", re.M)


def is_spec(f: SourceFile) -> bool:
    if f.language not in {"yaml", "json"}:
        return False
    return bool(_NAME.search(PurePosixPath(f.path).stem) or _KEY.search(f.text[:4000]))


def _load(f: SourceFile) -> dict | None:
    try:
        doc = json.loads(f.text) if f.language == "json" else yaml.safe_load(f.text)
    except (ValueError, yaml.YAMLError):
        return None
    return doc if isinstance(doc, dict) and ({"openapi", "swagger", "asyncapi"} & doc.keys()) else None


def _refs(node) -> set[str]:
    """Names of every schema or message referenced with $ref below node."""
    out: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and "/" in ref:
            out.add(ref.rsplit("/", 1)[-1])
        for v in node.values():
            out |= _refs(v)
    elif isinstance(node, list):
        for v in node:
            out |= _refs(v)
    return out


def _base_path(doc: dict) -> str:
    if "swagger" in doc:
        return doc.get("basePath") or ""
    servers = doc.get("servers")
    if not (isinstance(servers, list) and servers and isinstance(servers[0], dict)):
        return ""
    url = str(servers[0].get("url", ""))
    path = urlparse(url).path if "://" in url else url
    return path if path.startswith("/") else ""


def build(g: CodeGraph, files: list[SourceFile]) -> None:
    for f in files:
        if not is_spec(f):
            continue
        doc = _load(f)
        if doc is None:
            g.counters["contract_specs_invalid"] += 1
            continue
        g.counters["contract_specs"] += 1
        file_id = f"{g.repo}:{f.path}"
        g.nodes[file_id].props["spec"] = "asyncapi" if "asyncapi" in doc else "openapi"
        kind = "asyncapi" if "asyncapi" in doc else "openapi"
        title = str((doc.get("info") or {}).get("title") or PurePosixPath(f.path).stem)
        version = str((doc.get("info") or {}).get("version") or "")

        components = doc.get("components") or {}
        schemas = dict(doc.get("definitions") or {})                       # Swagger 2.0
        schemas.update(components.get("schemas") or {})
        if kind == "asyncapi":
            schemas.update(components.get("messages") or {})
        schema_ids = {}
        for name, body in schemas.items():
            sid = f"{file_id}#schema:{name}"
            schema_ids[name] = sid
            props = (body or {}).get("properties") or ((body or {}).get("payload") or {}).get("properties") or {}
            g.add_node(sid, ("Code", "Contract", "Schema"), name=str(name), path=f.path, spec=kind, api=title,
                       properties=sorted(map(str, props)) or None, required=(body or {}).get("required") or None,
                       text=json.dumps(body, default=str)[:TEXT_LIMIT])
            g.add_edge("DEFINES", file_id, sid, "parser")
        for name, body in schemas.items():
            for ref in _refs(body) - {name}:
                if ref in schema_ids:
                    g.add_edge("USES_SCHEMA", schema_ids[name], schema_ids[ref], "parser", role="property")

        def operation(key: str, method: str, template: str, op: dict, **extra) -> str:
            oid = f"{file_id}#{key}"
            request = _refs({k: op.get(k) for k in ("requestBody", "parameters", "message")})
            response = _refs(op.get("responses"))
            g.add_node(oid, ("Code", "Contract", "ApiOperation"), name=op.get("operationId") or key,
                       method=method, path_template=template, operation_id=op.get("operationId"),
                       summary=op.get("summary") or (op.get("description") or "")[:300] or None,
                       tags=op.get("tags") or None, path=f.path, spec=kind, api=title, api_version=version,
                       request_schemas=sorted(request) or None, response_schemas=sorted(response) or None,
                       deprecated=op.get("deprecated") or None, **extra)
            g.add_edge("DEFINES", file_id, oid, "parser")
            for role, names in (("request", request), ("response", response)):
                for n in names:
                    if n in schema_ids:
                        g.add_edge("USES_SCHEMA", oid, schema_ids[n], "parser", role=role)
            g.counters["contract_operations"] += 1
            return oid

        if kind == "openapi":
            base = _base_path(doc)
            for template, item in (doc.get("paths") or {}).items():
                if not isinstance(item, dict):
                    continue
                shared = item.get("parameters") or []
                for method, op in item.items():
                    if method.upper() not in VERBS or not isinstance(op, dict):
                        continue
                    full = join(base, str(template))
                    operation(f"{method.upper()} {full}", method.upper(), full,
                              {**op, "parameters": (op.get("parameters") or []) + shared},
                              route_key=norm_route(full))
        else:
            channels = doc.get("channels") or {}
            if str(doc["asyncapi"]).startswith("2"):
                for channel, item in channels.items():
                    for action in ("publish", "subscribe"):
                        if isinstance((item or {}).get(action), dict):
                            operation(f"{action.upper()} {channel}", action.upper(), str(channel),
                                      item[action], channel=str(channel))
            else:   # 3.x: operations reference channels; send = the app publishes, receive = it subscribes
                for op_id, op in (doc.get("operations") or {}).items():
                    if not isinstance(op, dict):
                        continue
                    ref = ((op.get("channel") or {}).get("$ref") or "").rsplit("/", 1)[-1]
                    address = (channels.get(ref) or {}).get("address") or ref
                    action = {"send": "PUBLISH", "receive": "SUBSCRIBE"}.get(op.get("action"), "PUBLISH")
                    operation(f"{action} {address}", action, str(address),
                              {"operationId": op_id, **op, "message": op.get("messages")}, channel=str(address))
