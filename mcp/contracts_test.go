package main

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestNormRoute(t *testing.T) {
	cases := map[string]string{
		"/api/Orders/{orderId}/": "/api/orders/{}",
		"/users/:id/x":           "/users/{}/x",
		"/a/<int:id>?q=1":        "/a/{}",
		"/orders/${id}":          "/orders/{}",
		"/":                      "/",
	}
	for in, want := range cases {
		if got := normRoute(in); got != want {
			t.Errorf("normRoute(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestEdgeListsAreDocumented(t *testing.T) {
	for _, e := range strings.Split(impactEdges+"|"+testEdges, "|") {
		if !edgeType.MatchString(e) {
			t.Errorf("edge type %q fails the edge-type guard", e)
		}
	}
	for _, e := range []string{"CALLS_HTTP", "TESTS", "PUBLISHES", "SUBSCRIBES"} {
		if !strings.Contains(contractDoc, e) {
			t.Errorf("contractDoc misses %s", e)
		}
	}
}

// Integration test against the fixture services of the pipeline (pipeline/tests/fixtures/services),
// ingested as orders-service and billing-service with kg-ingest-code (the links step runs at the end).
func TestContractsIntegration(t *testing.T) {
	if os.Getenv("NEO4J_PASSWORD") == "" {
		t.Skip("NEO4J_PASSWORD not set")
	}
	ctx := context.Background()
	graph, err := NewGraph(env("NEO4J_URI", "bolt://localhost:7687"), env("NEO4J_USER", "neo4j"), os.Getenv("NEO4J_PASSWORD"))
	if err != nil {
		t.Fatal(err)
	}
	defer graph.Close(ctx)
	if err := graph.Verify(ctx); err != nil {
		t.Skip("neo4j not reachable:", err)
	}
	if rows, _ := graph.Read(ctx, `MATCH (r:Repository) WHERE r.name IN ['orders-service', 'billing-service'] RETURN r.name`, nil); len(rows) != 2 {
		t.Skip("fixture services orders-service and billing-service are not ingested")
	}
	s := &Server{graph: graph, emb: NewEmbedder("http://localhost:1", "none", ""), groups: map[string]bool{"*": true}}
	st, ct := mcp.NewInMemoryTransports()
	if _, err := newMCPServer(s).Connect(ctx, st, nil); err != nil {
		t.Fatal(err)
	}
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil).Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	call := func(name string, args map[string]any) map[string]any {
		t.Helper()
		res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: name, Arguments: args})
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if res.IsError {
			t.Fatalf("%s returned error: %s", name, res.Content[0].(*mcp.TextContent).Text)
		}
		var out map[string]any
		b, _ := json.Marshal(res.StructuredContent)
		json.Unmarshal(b, &out)
		return out
	}
	ids := func(v any) string {
		var b strings.Builder
		list, _ := v.([]any)
		for _, r := range list {
			b.WriteString(r.(map[string]any)["id"].(string) + "\n")
		}
		return b.String()
	}

	op := call("get_contract", map[string]any{"contract": "createOrder"})["data"].(map[string]any)
	if !strings.Contains(ids(op["implementers"]), "orders-service:app/routers/orders.py#create_order") ||
		!strings.Contains(ids(op["tests"]), "test_create_order") || len(op["schemas"].([]any)) < 2 {
		t.Errorf("createOrder: %v", op)
	}
	inv := call("get_contract", map[string]any{"contract": "POST /invoices"})["data"].(map[string]any)
	if !strings.Contains(ids(inv["callers"]), "orders-service:app/billing.py#create_invoice") {
		t.Errorf("POST /invoices callers: %v", inv["callers"])
	}
	tbl := call("get_contract", map[string]any{"contract": "orders", "repo": "orders-service"})["data"].(map[string]any)
	if len(tbl["columns"].([]any)) != 3 || !strings.Contains(ids(tbl["readers"]), "load_order") {
		t.Errorf("table orders: %v", tbl)
	}
	topic := call("get_contract", map[string]any{"contract": "order-created"})["data"].(map[string]any)
	if ids(topic["publishers"]) == "" || ids(topic["subscribers"]) == "" || ids(topic["specs"]) == "" {
		t.Errorf("topic: %v", topic)
	}

	impact := call("impact_analysis", map[string]any{"symbol": "billing-service:src/routes.ts#createInvoice"})["data"].(map[string]any)
	if !strings.Contains(ids(impact["affected_code"]), "orders-service:app/billing.py#create_invoice") ||
		!strings.Contains(ids(impact["affected_tests"]), "billing-service:test/invoices.test.ts") {
		t.Errorf("impact of createInvoice: %v", impact)
	}
	pub := call("impact_analysis", map[string]any{"symbol": "orders-service:app/events.py#publish_order_created"})["data"].(map[string]any)
	if !strings.Contains(ids(pub["affected_code"]), "billing-service:src/consumer.ts#startConsumer") {
		t.Errorf("impact of the publisher misses the subscriber: %v", pub["affected_code"])
	}
	tests := call("find_related_tests", map[string]any{"symbol": "orders-service:app/routers/orders.py#get_order"})
	if !strings.Contains(ids(tests["results"]), "test_get_order") {
		t.Errorf("find_related_tests(get_order): %v", tests)
	}
}
