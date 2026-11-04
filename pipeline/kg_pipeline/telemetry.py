"""OpenTelemetry tracing for the pipeline: Dagster assets, LLM calls and embedding calls.

Off unless OTEL_EXPORTER_OTLP_ENDPOINT (or OTEL_EXPORTER_OTLP_TRACES_ENDPOINT) is set and the `otel`
extra is installed (pip install ".[otel]"). Spans go over OTLP/HTTP, so Arize Phoenix
(http://phoenix:6006) and Langfuse (https://<host>/api/public/otel, with OTEL_EXPORTER_OTLP_HEADERS)
both work. The standard variables apply: OTEL_SERVICE_NAME, OTEL_EXPORTER_OTLP_HEADERS,
OTEL_RESOURCE_ATTRIBUTES, OTEL_SDK_DISABLED.

LLM spans follow the GenAI conventions (gen_ai.*) plus the OpenInference span kind. Prompts and
answers are not recorded unless KG_TRACE_CONTENT=1, because they contain source code.
"""
from __future__ import annotations

import functools
import logging
import os
from contextlib import contextmanager

log = logging.getLogger(__name__)
CONTENT_CHARS = 4000
_state: dict = {}


def tracer():
    """The pipeline tracer, or None when tracing is off."""
    if "tracer" not in _state:
        _state["tracer"] = _setup()
    return _state["tracer"]


def _setup():
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint or os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry is not installed; pip install '.[otel]'")
        return None
    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "kg-pipeline")})
    provider = TracerProvider(resource=resource)  # flushes at interpreter exit
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return trace.get_tracer("kg_pipeline")


@contextmanager
def span(name: str, **attributes):
    """A child span of the current one; a no-op when tracing is off."""
    t = tracer()
    if t is None:
        yield None
        return
    with t.start_as_current_span(name, attributes={k: v for k, v in attributes.items() if v is not None}) as s:
        yield s


def _content() -> bool:
    return os.environ.get("KG_TRACE_CONTENT") == "1"


def traced_asset(fn):
    """Decorator for a Dagster asset function (put it under @dg.asset): one span per materialisation."""
    @functools.wraps(fn)
    def wrapper(context, *args, **kwargs):
        partition = context.partition_key if context.has_partition_key else None
        with span(f"asset {fn.__name__}", **{"dagster.asset": fn.__name__, "dagster.partition": partition,
                                              "dagster.run_id": context.run_id}):
            return fn(context, *args, **kwargs)
    return wrapper


def traced_llm(fn):
    """Decorator for LLM.json(self, system, user, schema)."""
    @functools.wraps(fn)
    def wrapper(self, system: str, user: str, schema: dict):
        attrs = {"openinference.span.kind": "LLM", "gen_ai.operation.name": "chat", "gen_ai.system": self.provider,
                 "gen_ai.request.model": self.model, "llm.model_name": self.model, "gen_ai.request.temperature": 0}
        if _content():
            attrs["input.value"] = (system + "\n\n" + user)[:CONTENT_CHARS]
        with span(f"chat {self.model}", **attrs) as s:
            out = fn(self, system, user, schema)
            if s is not None and _content():
                s.set_attribute("output.value", str(out)[:CONTENT_CHARS])
            return out
    return wrapper


def traced_embed(fn):
    """Decorator for Embedder._embed(self, texts)."""
    @functools.wraps(fn)
    def wrapper(self, texts: list[str]):
        with span(f"embeddings {self.model}", **{"openinference.span.kind": "EMBEDDING",
                                                  "gen_ai.operation.name": "embeddings", "gen_ai.request.model": self.model,
                                                  "embedding.model_name": self.model, "kg.texts": len(texts)}):
            return fn(self, texts)
    return wrapper
