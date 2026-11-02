"""Deterministic roles from annotations, decorators and supertypes (no LLM).

entity      JPA/Hibernate @Entity, TypeORM @Entity, SQLAlchemy/Django/SQLModel models, EF tables
endpoint    Spring @GetMapping..., ASP.NET [HttpGet], NestJS @Get, FastAPI/Flask @router.get / @app.route
controller  @RestController, @Controller, [ApiController], NestJS @Controller
repository  @Repository, Spring Data repositories, EF DbContext
service     @Service, NestJS @Injectable
dto         Pydantic BaseModel, marshmallow Schema, Java records annotated @Value / @Data are not assumed
"""
from __future__ import annotations

import re

_HTTP = r"(Get|Post|Put|Delete|Patch|Head|Options)"
ENDPOINT = [
    re.compile(rf"^{_HTTP}Mapping\b"), re.compile(r"^RequestMapping\b"),           # Spring
    re.compile(rf"^Http{_HTTP}\b"),                                                 # ASP.NET
    re.compile(rf"^{_HTTP}\("),                                                     # NestJS
    re.compile(r"\.(get|post|put|delete|patch|head|options|route|api_route|websocket)\("),  # FastAPI/Flask
    re.compile(r"^api_view\b"),                                                     # Django REST
]
ENTITY_ANN = {"Entity", "Table", "Document", "Embeddable", "MappedSuperclass", "Aggregate", "AggregateRoot"}
ENTITY_SUPER = {"Base", "DeclarativeBase", "Model", "SQLModel", "Document", "DbModel"}
CONTROLLER_ANN = {"RestController", "Controller", "ApiController"}
REPOSITORY_ANN = {"Repository"}
REPOSITORY_SUPER = {"JpaRepository", "CrudRepository", "PagingAndSortingRepository", "MongoRepository",
                    "ReactiveCrudRepository", "DbContext", "Repository"}
SERVICE_ANN = {"Service", "Injectable"}
DTO_SUPER = {"BaseModel", "Schema", "TypedDict"}
_STRING = re.compile(r"""["']([^"']*)["']""")
_METHOD = re.compile(r"(get|post|put|delete|patch|head|options)", re.I)


def _ann_name(annotation: str) -> str:
    """'Table(name="x")' -> 'Table'; 'router.get("/x")' -> 'router.get'."""
    return re.split(r"[(\s]", annotation, maxsplit=1)[0]


def classify(kind: str, annotations: list[str] | None, supertypes: list[str] | None) -> dict | None:
    annotations, supertypes = annotations or [], supertypes or []
    names = {_ann_name(a).split(".")[-1] for a in annotations}
    if kind in {"Function", "Method"}:
        for a in annotations:
            if any(p.search(a) for p in ENDPOINT):
                route = _STRING.search(a)
                verb = _METHOD.search(_ann_name(a))
                return {"role": "endpoint", "http_method": verb.group(1).upper() if verb else None,
                        "route": route.group(1) if route else None}
        return None
    if names & ENTITY_ANN or set(supertypes) & ENTITY_SUPER and kind == "Class":
        return {"role": "entity"}
    if names & CONTROLLER_ANN:
        route = next((_STRING.search(a) for a in annotations if _ann_name(a).endswith("Controller")), None)
        return {"role": "controller", "route": route.group(1) if route else None}
    if names & REPOSITORY_ANN or set(supertypes) & REPOSITORY_SUPER:
        return {"role": "repository"}
    if names & SERVICE_ANN:
        return {"role": "service"}
    if set(supertypes) & DTO_SUPER:
        return {"role": "dto"}
    return None
