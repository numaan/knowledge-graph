package main

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/neo4j/neo4j-go-driver/v5/neo4j"
)

func TestRankExperts(t *testing.T) {
	now := time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)
	team := &Server{groups: map[string]bool{"repo:shop": true}}
	rows := []map[string]any{
		{"person": "p:old", "name": "Old", "kind": "commit", "item": "c1", "date": "2025-05-01T00:00:00Z", "acl": []any{"repo:shop"}},
		{"person": "p:old", "name": "Old", "kind": "commit", "item": "c2", "date": "2025-05-02T00:00:00Z", "acl": []any{"repo:shop"}},
		{"person": "p:new", "name": "New", "kind": "pr_author", "item": "pr1", "date": "2026-10-30T00:00:00Z", "acl": []any{"repo:shop"}},
		{"person": "p:new", "name": "New", "kind": "pr_author", "item": "pr1", "date": "2026-10-30T00:00:00Z", "acl": []any{"repo:shop"}}, // duplicate
		{"person": "p:new", "name": "New", "kind": "ticket_assignee", "item": "KGT-1", "date": "2026-10-01T10:00:00.000+0000", "acl": []any{"jira:KGT"}},
		{"person": "p:hr", "name": "Hidden", "kind": "commit", "item": "c9", "date": "2026-10-31T00:00:00Z", "acl": []any{"repo:hr"}},
		{"person": "p:bot", "name": "dependabot", "bot": true, "kind": "commit", "item": "c8", "date": "2026-10-31T00:00:00Z"},
		{"person": "p:own", "name": "Owner", "kind": "code_owner", "item": "shop:a.py"},
	}
	got := rankExperts(rows, now, team.allowed, 10)
	names := []string{}
	for _, r := range got {
		names = append(names, r["name"].(string))
	}
	// owner 1.5 (no decay) > recent PR ~0.99 > two commits of 18 months ago (~0.25)
	if strings.Join(names, ",") != "Owner,New,Old" {
		t.Fatalf("order: %v", names)
	}
	if got[1]["counts"].(map[string]int)["pr_author"] != 1 || got[1]["counts"].(map[string]int)["ticket_assignee"] != 0 {
		t.Errorf("duplicates or hidden ticket counted: %v", got[1]["counts"])
	}
	if got[1]["last_activity"] != "2026-10-30" || len(got[2]["evidence"].([]map[string]any)) != 2 {
		t.Errorf("evidence: %+v", got)
	}
	if len(rankExperts(rows, now, team.allowed, 1)) != 1 {
		t.Error("limit not applied")
	}
	if _, ok := parseDate("2026-10-01T10:00:00.000+0000"); !ok {
		t.Error("Jira date not parsed")
	}
}

// End to end against a small graph that the test writes and deletes (repository it-mcp-people).
func TestPeopleToolsIntegration(t *testing.T) {
	if os.Getenv("NEO4J_PASSWORD") == "" {
		t.Skip("NEO4J_PASSWORD not set")
	}
	ctx := context.Background()
	uri, pw := env("NEO4J_URI", "bolt://localhost:7687"), os.Getenv("NEO4J_PASSWORD")
	d, err := neo4j.NewDriverWithContext(uri, neo4j.BasicAuth(env("NEO4J_USER", "neo4j"), pw, ""))
	if err != nil {
		t.Fatal(err)
	}
	defer d.Close(ctx)
	if err := d.VerifyConnectivity(ctx); err != nil {
		t.Skip("neo4j not reachable:", err)
	}
	write := func(q string) {
		t.Helper()
		if _, err := neo4j.ExecuteQuery(ctx, d, q, nil, neo4j.EagerResultTransformer); err != nil {
			t.Fatal(err)
		}
	}
	cleanup := `MATCH (n) WHERE n.repo = 'it-mcp-people' OR n.id STARTS WITH 'itp:' DETACH DELETE n`
	write(cleanup)
	defer write(cleanup)
	write(`
		CREATE (r:Code:Repository {id: 'it-mcp-people', name: 'it-mcp-people', repo: 'it-mcp-people'})
		CREATE (f:Code:File {id: 'it-mcp-people:src/cart.py', path: 'src/cart.py', repo: 'it-mcp-people'})
		CREATE (s:Code:Symbol {id: 'it-mcp-people:src/cart.py#save', name: 'save', qualname: 'Cart.save', repo: 'it-mcp-people', kind: 'Method'})
		CREATE (f)-[:DEFINES]->(s), (r)-[:CONTAINS]->(f)
		CREATE (c:Source:Commit {id: 'itp:c1', repo: 'it-mcp-people', sha: 'c1', title: 'Fix save', date: '2026-10-20T00:00:00Z', acl: ['repo:it-mcp-people']})
		CREATE (pr:Source:PullRequest {id: 'itp:pr1', key: 'acme/shop#1', title: 'Fix save', repo: 'it-mcp-people', merged_at: '2026-10-21T00:00:00Z',
		        url: 'https://github.com/acme/shop/pull/1', text: 'Why: duplicates', acl: ['repo:it-mcp-people']})
		CREATE (c)-[:MODIFIES]->(s), (c)-[:MODIFIES]->(f), (pr)-[:MODIFIES]->(f), (pr)-[:CONTAINS]->(c)
		CREATE (a:Source:Person {id: 'itp:alice', name: 'Alice Smith', title: 'Alice Smith', bot: false, acl: ['repo:it-mcp-people']})
		CREATE (b:Source:Person {id: 'itp:bob', name: 'Bob', title: 'Bob', bot: false, acl: ['repo:it-mcp-people']})
		CREATE (a)-[:AUTHORED]->(c), (a)-[:AUTHORED]->(pr), (b)-[:REVIEWED]->(pr)
		CREATE (tm:Source:Team {id: 'itp:team', name: 'team-cart', title: 'Cart team', repo: 'it-mcp-people', acl: ['repo:it-mcp-people']})
		CREATE (svc:Source:Service {id: 'itp:svc', name: 'cart', title: 'cart', repo: 'it-mcp-people', acl: ['repo:it-mcp-people']})
		CREATE (tm)-[:OWNS {via: 'codeowners', pattern: '/src/'}]->(f), (tm)-[:OWNS {via: 'catalog'}]->(svc)
		CREATE (svc)-[:IMPLEMENTED_IN]->(r), (b)-[:MEMBER_OF]->(tm)`)

	graph, err := NewGraph(uri, env("NEO4J_USER", "neo4j"), pw)
	if err != nil {
		t.Fatal(err)
	}
	defer graph.Close(ctx)
	connect := func(groups map[string]bool) *mcp.ClientSession {
		s := &Server{graph: graph, emb: NewEmbedder(env("EMBEDDINGS_URL", "http://localhost:11434"), env("EMBEDDINGS_MODEL", "qwen3-embedding:0.6b"), ""),
			groups: groups}
		graph.fixed["repos"] = repoScope(groups)
		st, ct := mcp.NewInMemoryTransports()
		if _, err := newMCPServer(s).Connect(ctx, st, nil); err != nil {
			t.Fatal(err)
		}
		cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil).Connect(ctx, ct, nil)
		if err != nil {
			t.Fatal(err)
		}
		return cs
	}
	call := func(cs *mcp.ClientSession, name string, args map[string]any) (map[string]any, bool) {
		t.Helper()
		res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: name, Arguments: args})
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		var out map[string]any
		b, _ := json.Marshal(res.StructuredContent)
		json.Unmarshal(b, &out)
		return out, res.IsError
	}

	cs := connect(map[string]bool{"*": true})
	ex, _ := call(cs, "find_experts", map[string]any{"node": "Cart.save"})
	res := ex["results"].([]any)
	if len(res) < 2 || res[0].(map[string]any)["name"] != "Alice Smith" {
		t.Fatalf("find_experts: %v", ex)
	}
	b, _ := json.Marshal(ex)
	if !strings.Contains(string(b), `"review"`) || !strings.Contains(string(b), `"team_owner"`) {
		t.Errorf("find_experts misses review or team evidence: %s", b)
	}
	own, isErr := call(cs, "get_owners", map[string]any{"node": "src/cart.py"})
	b, _ = json.Marshal(own)
	if isErr || !strings.Contains(string(b), "Cart team") || !strings.Contains(string(b), `"pattern":"/src/"`) ||
		!strings.Contains(string(b), `"name":"Bob"`) {
		t.Errorf("get_owners: %s", b)
	}
	svc, _ := call(cs, "get_owners", map[string]any{"node": "cart"})
	if b, _ = json.Marshal(svc); !strings.Contains(string(b), `"kind":"Service"`) || !strings.Contains(string(b), "Cart team") {
		t.Errorf("get_owners for a service: %s", b)
	}
	h, _ := call(cs, "get_change_history", map[string]any{"symbol": "src/cart.py"})
	if b, _ = json.Marshal(h); !strings.Contains(string(b), "acme/shop#1: Fix save") {
		t.Errorf("get_change_history has no pull request: %s", b)
	}
	cs.Close()

	// A server for another repository sees no people and no owners of this code.
	cs = connect(map[string]bool{"repo:other": true})
	defer cs.Close()
	if _, isErr := call(cs, "find_experts", map[string]any{"node": "src/cart.py"}); !isErr {
		t.Error("find_experts resolved code outside the scope")
	}
	if out, isErr := call(cs, "get_owners", map[string]any{"node": "cart"}); !isErr {
		if b, _ := json.Marshal(out); strings.Contains(string(b), "Cart team") {
			t.Errorf("get_owners leaked: %s", b)
		}
	}
}
