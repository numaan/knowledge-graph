package main

import (
	"context"
	"errors"
	"io"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

type echoIn struct {
	Text string `json:"text"`
}

// The middleware must count calls by status, record latency and make one span per tool call.
func TestObserveMiddleware(t *testing.T) {
	spans := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(spans))
	otel.SetTracerProvider(tp)
	defer tp.Shutdown(context.Background())

	ctx := context.Background()
	m := mcp.NewServer(&mcp.Implementation{Name: "t", Version: "0"}, nil)
	mcp.AddTool(m, &mcp.Tool{Name: "echo_test"}, func(ctx context.Context, _ *mcp.CallToolRequest, in echoIn) (*mcp.CallToolResult, Doc, error) {
		_, span, start := childSpan(ctx, "neo4j.read")
		endSpan(span, start, querySeconds, nil)
		if in.Text == "fail" {
			return nil, Doc{}, errors.New("boom")
		}
		return nil, Doc{Data: map[string]any{"text": in.Text}}, nil
	})
	m.AddReceivingMiddleware(observe)
	st, ct := mcp.NewInMemoryTransports()
	if _, err := m.Connect(ctx, st, nil); err != nil {
		t.Fatal(err)
	}
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "c", Version: "0"}, nil).Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	for _, text := range []string{"hi", "hi", "fail"} {
		if _, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "echo_test", Arguments: map[string]any{"text": text}}); err != nil {
			t.Fatal(err)
		}
	}

	rec := httptest.NewRecorder()
	metricsHandler().ServeHTTP(rec, httptest.NewRequest("GET", "/metrics", nil))
	body, _ := io.ReadAll(rec.Body)
	for _, want := range []string{
		`kg_mcp_tool_calls_total{status="ok",tool="echo_test"} 2`,
		`kg_mcp_tool_calls_total{status="tool_error",tool="echo_test"} 1`,
		`kg_mcp_tool_errors_total{tool="echo_test"} 1`,
		`kg_mcp_tool_duration_seconds_count{tool="echo_test"} 3`,
		`kg_mcp_neo4j_query_duration_seconds_count{status="ok"}`,
	} {
		if !strings.Contains(string(body), want) {
			t.Errorf("metrics lack %s", want)
		}
	}

	var tools, children int
	for _, s := range spans.GetSpans() {
		switch s.Name {
		case "tool echo_test":
			tools++
		case "neo4j.read":
			children++
			if !s.Parent.IsValid() {
				t.Error("neo4j.read span has no parent tool span")
			}
		}
	}
	if tools != 3 || children != 3 {
		t.Errorf("want 3 tool spans and 3 child spans, got %d and %d", tools, children)
	}
}

func TestSetupTracingOffWithoutEndpoint(t *testing.T) {
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
	if err := setupTracing(context.Background())(context.Background()); err != nil {
		t.Fatal(err)
	}
}
