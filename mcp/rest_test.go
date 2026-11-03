package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type memSink struct {
	mu  sync.Mutex
	got []Feedback
}

func (m *memSink) Submit(_ context.Context, f Feedback) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.got = append(m.got, f)
	return nil
}

type WhoIn struct {
	Echo string `json:"echo,omitempty"`
}

// restFixture serves the REST layer over fake tools, without Neo4j.
func restFixture(t *testing.T) (*httptest.Server, *REST, *memSink) {
	t.Helper()
	m := mcp.NewServer(&mcp.Implementation{Name: "kg", Version: "test"}, nil)
	mcp.AddTool(m, &mcp.Tool{Name: "whoami", Description: "caller of the request"},
		func(ctx context.Context, _ *mcp.CallToolRequest, in WhoIn) (*mcp.CallToolResult, Doc, error) {
			data := map[string]any{"echo": in.Echo}
			if c := CallerFrom(ctx); c != nil {
				data["subject"] = c.Subject
			}
			return nil, Doc{Data: data}, nil
		})
	mcp.AddTool(m, &mcp.Tool{Name: "broken", Description: "always fails"},
		func(context.Context, *mcp.CallToolRequest, WhoIn) (*mcp.CallToolResult, Doc, error) {
			return nil, Doc{}, errors.New("no such ticket")
		})
	mcp.AddTool(m, &mcp.Tool{Name: "get_neighbors", Description: "fake"},
		func(_ context.Context, _ *mcp.CallToolRequest, in NeighborsIn) (*mcp.CallToolResult, Rows, error) {
			return nil, Rows{Results: []map[string]any{{"id": "n2", "via": []any{"CALLS"}, "from": in.NodeID}}}, nil
		})
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	api, err := newREST(ctx, nil, m)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { api.Close() })
	sink := &memSink{}
	api.Feedback = sink
	api.node = func(_ context.Context, id string) (map[string]any, error) {
		if id == "precept:a.ts#A" {
			return map[string]any{"labels": []any{"Code", "Symbol"}, "properties": map[string]any{"id": id}}, nil
		}
		return nil, nil
	}
	api.Identity = func(r *http.Request) (*Caller, error) {
		if r.Header.Get("Authorization") == "Bearer bad" {
			return nil, errors.New("invalid token")
		}
		return &Caller{Subject: "alice"}, nil
	}
	mux := http.NewServeMux()
	api.Mount(mux)
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv, api, sink
}

func do(t *testing.T, method, url, body string, header ...string) (int, map[string]any) {
	t.Helper()
	req, _ := http.NewRequest(method, url, strings.NewReader(body))
	for i := 0; i+1 < len(header); i += 2 {
		req.Header.Set(header[i], header[i+1])
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	b, _ := io.ReadAll(res.Body)
	var out map[string]any
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatalf("%s %s: not JSON: %s", method, url, b)
	}
	return res.StatusCode, out
}

func TestRESTTools(t *testing.T) {
	srv, _, _ := restFixture(t)
	code, out := do(t, "GET", srv.URL+"/api/tools", "")
	if code != 200 {
		t.Fatalf("list: %d %v", code, out)
	}
	names := map[string]bool{}
	for _, x := range out["tools"].([]any) {
		tool := x.(map[string]any)
		names[tool["name"].(string)] = true
		if tool["input_schema"] == nil {
			t.Errorf("%s: no input schema", tool["name"])
		}
	}
	if !names["whoami"] || !names["get_neighbors"] || len(names) != 3 {
		t.Errorf("tools: %v", names)
	}

	code, out = do(t, "POST", srv.URL+"/api/tools/whoami", `{"echo": "hi"}`)
	data, _ := out["data"].(map[string]any)
	if code != 200 || data["echo"] != "hi" || data["subject"] != "alice" {
		t.Errorf("call: %d %v (caller must reach the tool handler)", code, out)
	}
	if code, _ = do(t, "POST", srv.URL+"/api/tools/whoami", ""); code != 200 {
		t.Errorf("empty body must mean no arguments, got %d", code)
	}
	if code, out = do(t, "POST", srv.URL+"/api/tools/ask", `{}`); code != 404 || !strings.Contains(out["error"].(string), "not configured") {
		t.Errorf("missing tool: %d %v", code, out)
	}
	if code, out = do(t, "POST", srv.URL+"/api/tools/broken", `{}`); code != 422 || !strings.Contains(out["error"].(string), "no such ticket") {
		t.Errorf("tool error: %d %v", code, out)
	}
	if code, _ = do(t, "POST", srv.URL+"/api/tools/whoami", `[1,2]`); code != 400 {
		t.Errorf("non-object body: %d", code)
	}
	if code, _ = do(t, "POST", srv.URL+"/api/tools/whoami", `{"echo": 5}`); code != 400 && code != 422 {
		t.Errorf("schema violation: %d", code)
	}
	if code, _ = do(t, "GET", srv.URL+"/api/tools", "", "Authorization", "Bearer bad"); code != 401 {
		t.Errorf("identity hook error must give 401, got %d", code)
	}
}

func TestRESTNode(t *testing.T) {
	srv, _, _ := restFixture(t)
	code, out := do(t, "GET", srv.URL+"/api/node/precept:a.ts%23A", "")
	if code != 200 {
		t.Fatalf("node: %d %v", code, out)
	}
	nb := out["neighbors"].(map[string]any)["results"].([]any)
	if out["node"].(map[string]any)["labels"] == nil || nb[0].(map[string]any)["from"] != "precept:a.ts#A" {
		t.Errorf("node: %v", out)
	}
	if code, _ = do(t, "GET", srv.URL+"/api/node/hidden", ""); code != 404 {
		t.Errorf("hidden node: %d", code)
	}
}

func TestRESTFeedback(t *testing.T) {
	srv, _, sink := restFixture(t)
	code, _ := do(t, "POST", srv.URL+"/api/feedback", `{"node_id": "jira:PREC-1", "kind": "outdated", "comment": " fixed in 2.1 "}`)
	if code != 202 || len(sink.got) != 1 {
		t.Fatalf("feedback: %d %v", code, sink.got)
	}
	f := sink.got[0]
	if f.Kind != "outdated" || f.Comment != "fixed in 2.1" || f.Caller.Subject != "alice" || f.At.IsZero() {
		t.Errorf("stored: %+v", f)
	}
	for _, bad := range []string{`{"node_id": "x", "kind": "spam"}`, `{"kind": "wrong"}`, `not json`,
		`{"node_id": "x", "kind": "wrong", "comment": "` + strings.Repeat("a", 4001) + `"}`} {
		if code, _ := do(t, "POST", srv.URL+"/api/feedback", bad); code != 400 {
			t.Errorf("accepted %.40s: %d", bad, code)
		}
	}
	big := bytes.Repeat([]byte("a"), 2<<20)
	if code, _ := do(t, "POST", srv.URL+"/api/feedback", `{"node_id": "`+string(big)+`"}`); code != 400 {
		t.Errorf("body over 1 MiB: %d", code)
	}
}

// An MCP client cannot set the caller through _meta; only the REST session can.
func TestRESTCallerNotSpoofable(t *testing.T) {
	m := mcp.NewServer(&mcp.Implementation{Name: "kg", Version: "test"}, nil)
	mcp.AddTool(m, &mcp.Tool{Name: "whoami"}, func(ctx context.Context, _ *mcp.CallToolRequest, _ WhoIn) (*mcp.CallToolResult, Doc, error) {
		return nil, Doc{Data: map[string]any{"caller": CallerFrom(ctx)}}, nil
	})
	ctx := context.Background()
	api, err := newREST(ctx, nil, m)
	if err != nil {
		t.Fatal(err)
	}
	defer api.Close()
	st, ct := mcp.NewInMemoryTransports()
	if _, err := m.Connect(ctx, st, nil); err != nil {
		t.Fatal(err)
	}
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "agent", Version: "0"}, nil).Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "whoami", Meta: mcp.Meta{callerMeta: map[string]any{"subject": "admin"}}})
	if err != nil {
		t.Fatal(err)
	}
	if b, _ := json.Marshal(res.StructuredContent); strings.Contains(string(b), "admin") {
		t.Errorf("MCP client spoofed the caller: %s", b)
	}
}
