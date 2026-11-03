package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// Tracing and metrics. Tracing is off unless OTEL_EXPORTER_OTLP_ENDPOINT (or
// OTEL_EXPORTER_OTLP_TRACES_ENDPOINT) is set; the exporter reads the standard OTEL_* variables
// (headers, service name, resource attributes), so Arize Phoenix and Langfuse both work.
// Spans: one per tool call, with Neo4j queries and embedding calls as children. Tool arguments
// are recorded only when KG_TRACE_CONTENT=1. Metrics are always on at /metrics.

var tracer = otel.Tracer("kg-mcp") // delegates to the provider set by setupTracing

var (
	toolCalls = prometheus.NewCounterVec(prometheus.CounterOpts{Name: "kg_mcp_tool_calls_total",
		Help: "MCP tool calls by tool and status (ok, tool_error, error)."}, []string{"tool", "status"})
	toolSeconds = prometheus.NewHistogramVec(prometheus.HistogramOpts{Name: "kg_mcp_tool_duration_seconds",
		Help: "MCP tool call latency.", Buckets: []float64{.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30}}, []string{"tool"})
	toolErrors = prometheus.NewCounterVec(prometheus.CounterOpts{Name: "kg_mcp_tool_errors_total",
		Help: "MCP tool calls that returned an error."}, []string{"tool"})
	querySeconds = prometheus.NewHistogramVec(prometheus.HistogramOpts{Name: "kg_mcp_neo4j_query_duration_seconds",
		Help: "Neo4j read transaction latency.", Buckets: prometheus.DefBuckets}, []string{"status"})
	embedSeconds = prometheus.NewHistogramVec(prometheus.HistogramOpts{Name: "kg_mcp_embedding_duration_seconds",
		Help: "Embedding call latency.", Buckets: prometheus.DefBuckets}, []string{"status"})
	metrics = prometheus.NewRegistry()
)

func init() {
	metrics.MustRegister(toolCalls, toolSeconds, toolErrors, querySeconds, embedSeconds,
		prometheus.NewGoCollector(), prometheus.NewProcessCollector(prometheus.ProcessCollectorOpts{}))
}

func metricsHandler() http.Handler { return promhttp.HandlerFor(metrics, promhttp.HandlerOpts{}) }

// setupTracing installs the OTLP/HTTP exporter; the returned function flushes it.
func setupTracing(ctx context.Context) func(context.Context) error {
	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") == "" && os.Getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") == "" ||
		strings.EqualFold(os.Getenv("OTEL_SDK_DISABLED"), "true") {
		return func(context.Context) error { return nil }
	}
	exp, err := otlptracehttp.New(ctx)
	if err != nil {
		log.Printf("warning: tracing off: %v", err)
		return func(context.Context) error { return nil }
	}
	res, _ := resource.Merge(resource.Default(), resource.NewSchemaless(
		attribute.String("service.name", env("OTEL_SERVICE_NAME", "kg-mcp")), attribute.String("service.version", version)))
	tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exp), sdktrace.WithResource(res))
	otel.SetTracerProvider(tp)
	return tp.Shutdown
}

// observe is MCP middleware: a span, a counter and a latency sample per tool call.
func observe(next mcp.MethodHandler) mcp.MethodHandler {
	return func(ctx context.Context, method string, req mcp.Request) (mcp.Result, error) {
		call, ok := req.(*mcp.CallToolRequest)
		if method != "tools/call" || !ok || call.Params == nil {
			return next(ctx, method, req)
		}
		name := call.Params.Name
		attrs := []attribute.KeyValue{attribute.String("gen_ai.operation.name", "execute_tool"),
			attribute.String("gen_ai.tool.name", name), attribute.String("openinference.span.kind", "TOOL")}
		if os.Getenv("KG_TRACE_CONTENT") == "1" {
			attrs = append(attrs, attribute.String("input.value", clip(string(call.Params.Arguments), 4000)))
		}
		ctx, span := tracer.Start(ctx, "tool "+name, trace.WithAttributes(attrs...))
		defer span.End()
		start := time.Now()
		res, err := next(ctx, method, req)
		status := "ok"
		if err != nil {
			status = "error"
			span.RecordError(err)
		} else if r, ok := res.(*mcp.CallToolResult); ok && r.IsError {
			status = "tool_error"
			if len(r.Content) > 0 {
				if t, ok := r.Content[0].(*mcp.TextContent); ok {
					span.SetAttributes(attribute.String("error.message", clip(t.Text, 500)))
				}
			}
		}
		if status != "ok" {
			span.SetStatus(codes.Error, status)
			toolErrors.WithLabelValues(name).Inc()
		}
		toolCalls.WithLabelValues(name, status).Inc()
		toolSeconds.WithLabelValues(name).Observe(time.Since(start).Seconds())
		return res, err
	}
}

// childSpan starts a span for a dependency call; end it with endSpan.
func childSpan(ctx context.Context, name string, attrs ...attribute.KeyValue) (context.Context, trace.Span, time.Time) {
	ctx, span := tracer.Start(ctx, name, trace.WithSpanKind(trace.SpanKindClient), trace.WithAttributes(attrs...))
	return ctx, span, time.Now()
}

func endSpan(span trace.Span, start time.Time, hist *prometheus.HistogramVec, err error) {
	status := "ok"
	if err != nil {
		status = "error"
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
	}
	if hist != nil {
		hist.WithLabelValues(status).Observe(time.Since(start).Seconds())
	}
	span.End()
}

// LLMSpan is for tools that call an LLM (GenAI and OpenInference attributes).
func LLMSpan(ctx context.Context, provider, model string) (context.Context, trace.Span, time.Time) {
	return childSpan(ctx, "chat "+model, attribute.String("gen_ai.operation.name", "chat"),
		attribute.String("gen_ai.system", provider), attribute.String("gen_ai.request.model", model),
		attribute.String("openinference.span.kind", "LLM"))
}

func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
