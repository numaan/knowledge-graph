package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// A REST caller gets its own mapped groups in tool calls (through the access middleware) and in
// the direct graph reads of the REST layer (node, requirements), never the server groups.
func TestRESTCallerAccess(t *testing.T) {
	s := &Server{groups: map[string]bool{"jira:HR": true, "jira:PREC": true}}
	s.requireCaller = true
	s.mapping = &aclMapping{Groups: map[string][]string{"team-hr": {"jira:HR"}}}
	m := mcp.NewServer(&mcp.Implementation{Name: "test", Version: "0"}, nil)
	m.AddReceivingMiddleware(s.accessMiddleware)
	mcp.AddTool(m, &mcp.Tool{Name: "groups", Description: "groups of the caller"},
		func(ctx context.Context, _ *mcp.CallToolRequest, _ WhoIn) (*mcp.CallToolResult, Doc, error) {
			return nil, Doc{Data: map[string]any{"groups": strings.Join(sortedGroups(s.groupsFor(ctx)), ",")}}, nil
		})
	mcp.AddTool(m, &mcp.Tool{Name: "get_neighbors", Description: "fake"},
		func(context.Context, *mcp.CallToolRequest, NeighborsIn) (*mcp.CallToolResult, Rows, error) {
			return nil, Rows{Results: []map[string]any{}}, nil
		})
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	api, err := newREST(ctx, s, m)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { api.Close() })
	var nodeGroups string
	api.node = func(ctx context.Context, id string) (map[string]any, error) {
		nodeGroups = strings.Join(sortedGroups(s.groupsFor(ctx)), ",")
		return map[string]any{"labels": []any{"Ticket"}, "properties": map[string]any{"id": id}}, nil
	}
	api.Identity = func(*http.Request) (*Caller, error) {
		return &Caller{Subject: "u-dana", Name: "dana", Groups: []string{"/team-hr"}}, nil
	}
	mux := http.NewServeMux()
	api.Mount(mux)
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)

	code, out := do(t, "POST", srv.URL+"/api/tools/groups", `{}`)
	if code != http.StatusOK {
		t.Fatalf("tool call: %d %v", code, out)
	}
	if data, _ := out["data"].(map[string]any); data["groups"] != "jira:HR" {
		t.Errorf("tool call groups: %v", out)
	}
	if code, out = do(t, "GET", srv.URL+"/api/node/jira:HR-1", ""); code != http.StatusOK {
		t.Fatalf("node: %d %v", code, out)
	}
	if nodeGroups != "jira:HR" {
		t.Errorf("node read groups: %q", nodeGroups)
	}
}
