package main

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestCheckReadOnly(t *testing.T) {
	ok := []string{
		"MATCH (n:Symbol) RETURN n.name LIMIT 5",
		"MATCH (n) WHERE n.name = 'CREATE user' RETURN n", // keyword inside a string
		"// DELETE everything\nMATCH (n) RETURN count(n)", // keyword inside a comment
		"CALL db.index.fulltext.queryNodes('symbol_text', 'invite') YIELD node RETURN node",
		"MATCH (n) WHERE n.offset > 1 RETURN n.reset", // SET inside a longer word
	}
	for _, q := range ok {
		if err := checkReadOnly(q); err != nil {
			t.Errorf("rejected read query %q: %v", q, err)
		}
	}
	bad := []string{
		"MATCH (n) DETACH DELETE n",
		"MATCH (n) SET n.x = 1",
		"merge (n:X {id: 1})",
		"LOAD CSV FROM 'file:///etc/passwd' AS r RETURN r",
		"CALL apoc.load.json('http://x') YIELD value RETURN value",
		"CALL dbms.security.listUsers()",
		"MATCH (n) FOREACH (x IN [1] | REMOVE n.a)",
	}
	for _, q := range bad {
		if err := checkReadOnly(q); err == nil {
			t.Errorf("accepted write or unsafe query %q", q)
		}
	}
}

func TestCheckEdgeTypes(t *testing.T) {
	if err := checkEdgeTypes([]string{"CALLS", "IMPLEMENTED_BY"}); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{"CALLS]->(x) DELETE x//", "calls", "A|B"} {
		if checkEdgeTypes([]string{bad}) == nil {
			t.Errorf("accepted edge type %q", bad)
		}
	}
}

func TestRRF(t *testing.T) {
	got := rrf([]string{"a", "b", "c"}, []string{"b", "a"}, []string{"b"})
	if got[0].ID != "b" || got[1].ID != "a" || got[2].ID != "c" {
		t.Fatalf("wrong order: %+v", got)
	}
	if len(rrf()) != 0 {
		t.Fatal("empty input must give empty output")
	}
}

func TestLucene(t *testing.T) {
	if got := lucene("who can invite users (bulk)?"); got != "who OR can OR invite OR users OR bulk" {
		t.Fatalf("got %q", got)
	}
	if got := lucene("a b"); got != "*" {
		t.Fatalf("short words must give match-all, got %q", got)
	}
}

// Integration test: real MCP client over in-memory transports against the local graph.
// Needs NEO4J_PASSWORD and an ingested repo (KG_TEST_REPO, default precept).
func TestToolsIntegration(t *testing.T) {
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
	repo := env("KG_TEST_REPO", "precept")
	s := &Server{graph: graph, emb: NewEmbedder(env("EMBEDDINGS_URL", "http://localhost:11434"),
		env("EMBEDDINGS_MODEL", "qwen3-embedding:0.6b"), ""), defaultRepo: repo, groups: map[string]bool{"*": true}}

	st, ct := mcp.NewInMemoryTransports()
	if _, err := newMCPServer(s).Connect(ctx, st, nil); err != nil {
		t.Fatal(err)
	}
	client := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil)
	cs, err := client.Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()

	tools, err := cs.ListTools(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(tools.Tools) != 15 {
		t.Errorf("want 15 tools, got %d", len(tools.Tools))
	}

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

	ov := call("domain_overview", map[string]any{})
	if len(ov["data"].(map[string]any)["concepts"].([]any)) == 0 {
		t.Error("domain_overview: no concepts")
	}
	syms := call("find_symbol", map[string]any{"name": "Invit*", "kind": "Class"})["results"].([]any)
	if len(syms) == 0 {
		t.Fatal("find_symbol: nothing for Invit*")
	}
	id := syms[0].(map[string]any)["id"].(string)
	code := call("get_symbol_code", map[string]any{"symbol": id})["data"].(map[string]any)
	if !strings.Contains(code["code"].(string), "class") {
		t.Errorf("get_symbol_code: no class in code for %s", id)
	}
	call("impact_analysis", map[string]any{"symbol": id, "depth": 2})
	call("find_related_tests", map[string]any{"symbol": id})
	if n := call("get_neighbors", map[string]any{"node_id": id, "edge_types": []string{"DEFINES", "CONTAINS"}}); len(n["results"].([]any)) == 0 {
		t.Error("get_neighbors: nothing")
	}
	hits := call("search_graph", map[string]any{"query": "invite users to a workspace", "k": 5})["results"].([]any)
	if len(hits) == 0 {
		t.Error("search_graph: no results")
	}
	rows := call("read_cypher", map[string]any{"query": "MATCH (n:Symbol {repo: $r}) RETURN count(n) AS n", "params": map[string]any{"r": repo}})
	if rows["results"].([]any)[0].(map[string]any)["n"].(float64) == 0 {
		t.Error("read_cypher: zero symbols")
	}
	res, _ := cs.CallTool(ctx, &mcp.CallToolParams{Name: "read_cypher", Arguments: map[string]any{"query": "MATCH (n) DETACH DELETE n"}})
	if !res.IsError {
		t.Error("read_cypher accepted a write")
	}

	// sources: git history and documents (skipped when those layers are not ingested)
	if h := call("get_change_history", map[string]any{"symbol": "ScenariosView"}); len(h["results"].([]any)) == 0 {
		t.Log("get_change_history: no commits (run kg-git-history)")
	}
	if k := call("search_knowledge", map[string]any{"query": "scenario builder fault types", "kind": "Section"}); len(k["results"].([]any)) == 0 {
		t.Log("search_knowledge: no sections (run kg-docs and kg-embed --sources)")
	}

	// summaries (skipped when kg-summaries did not run for this repository)
	res, err = cs.CallTool(ctx, &mcp.CallToolParams{Name: "get_summary", Arguments: map[string]any{"node_id": id}})
	if err != nil {
		t.Fatal(err)
	}
	if res.IsError {
		t.Log("get_summary: no summaries (run kg-summaries --repo " + repo + ")")
	} else if sum := call("get_summary", map[string]any{"node_id": id})["data"].(map[string]any)["summary"].(map[string]any); sum["text"] == "" {
		t.Error("get_summary: empty text")
	}
	if res, _ := cs.CallTool(ctx, &mcp.CallToolParams{Name: "get_summary", Arguments: map[string]any{"path": "no/such/folder"}}); !res.IsError {
		if d := call("get_summary", map[string]any{"path": "no/such/folder"})["data"].(map[string]any); d["note"] == nil ||
			d["summary"].(map[string]any)["target_id"] != repo+":folder:/" {
			t.Errorf("get_summary: unknown path must fall back to the root folder summary: %v", d)
		}
	}
}

func TestACLScope(t *testing.T) {
	all := &Server{groups: map[string]bool{"*": true}}
	team := &Server{groups: map[string]bool{"repo:precept": true, "jira:PREC": true}}
	if repoScope(all.groups) != nil {
		t.Error("* must not restrict repositories")
	}
	if got := repoScope(team.groups).([]string); len(got) != 1 || got[0] != "precept" {
		t.Errorf("scope: %v", got)
	}
	cases := []struct {
		acl  any
		want bool
	}{{nil, true}, {[]any{}, true}, {[]any{"jira:PREC"}, true}, {[]any{"jira:HR", "confluence:HR"}, false}}
	for _, c := range cases {
		if team.allowed(c.acl) != c.want {
			t.Errorf("allowed(%v) = %v", c.acl, !c.want)
		}
	}
	if !all.allowed([]any{"jira:HR"}) || team.allowedRepo("other") || !team.allowedRepo("precept") {
		t.Error("allowedRepo or * wrong")
	}
}

// A server scoped to another repository must not see precept code, and read_cypher is off.
func TestACLIntegration(t *testing.T) {
	if os.Getenv("NEO4J_PASSWORD") == "" {
		t.Skip("NEO4J_PASSWORD not set")
	}
	ctx := context.Background()
	graph, err := NewGraph(env("NEO4J_URI", "bolt://localhost:7687"), env("NEO4J_USER", "neo4j"), os.Getenv("NEO4J_PASSWORD"))
	if err != nil {
		t.Fatal(err)
	}
	defer graph.Close(ctx)
	s := &Server{graph: graph, emb: NewEmbedder(env("EMBEDDINGS_URL", "http://localhost:11434"),
		env("EMBEDDINGS_MODEL", "qwen3-embedding:0.6b"), ""), groups: map[string]bool{"repo:other": true}}
	graph.fixed["repos"] = repoScope(s.groups)
	st, ct := mcp.NewInMemoryTransports()
	if _, err := newMCPServer(s).Connect(ctx, st, nil); err != nil {
		t.Fatal(err)
	}
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil).Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	check := func(name string, args map[string]any) *mcp.CallToolResult {
		res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: name, Arguments: args})
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		return res
	}
	for _, c := range []struct {
		name string
		args map[string]any
	}{
		{"find_symbol", map[string]any{"name": "ScenariosView", "repo": "precept"}},
		{"search_graph", map[string]any{"query": "invite users", "repo": "precept"}},
	} {
		res := check(c.name, c.args)
		b, _ := json.Marshal(res.StructuredContent)
		if !res.IsError && strings.Contains(string(b), "precept:") {
			t.Errorf("%s leaked precept code: %.200s", c.name, b)
		}
	}
	for _, name := range []string{"get_symbol_code", "impact_analysis"} {
		if res := check(name, map[string]any{"symbol": "ScenariosView"}); !res.IsError {
			t.Errorf("%s resolved a symbol outside the scope", name)
		}
	}
	if res := check("domain_overview", map[string]any{"repo": "precept"}); !res.IsError {
		t.Error("domain_overview showed a repository outside the scope")
	}
	if res := check("read_cypher", map[string]any{"query": "MATCH (n) RETURN count(n)"}); !res.IsError {
		t.Error("read_cypher must be off for a scoped server")
	}
}

func TestCollapseDuplicates(t *testing.T) {
	rows := []map[string]any{
		{"id": "doc:a#s0", "canonical": "confluence:X:1#s0", "title": "A", "url": "file:///a"},
		{"id": "jira:X-1", "canonical": "jira:X-1", "title": "T"},
		{"id": "confluence:X:1#s0", "canonical": "confluence:X:1#s0", "title": "C", "url": "https://c"},
		{"id": "doc:b#s0", "title": "B"},
		{"id": "doc:c#s0", "canonical": "doc:c#s0", "title": "C2"},
	}
	got := collapseDuplicates(rows, 3)
	if len(got) != 3 || got[0]["id"] != "doc:a#s0" || got[1]["id"] != "jira:X-1" || got[2]["id"] != "doc:b#s0" {
		t.Fatalf("wrong rows: %+v", got)
	}
	also, _ := got[0]["also_in"].([]map[string]any)
	if len(also) != 1 || also[0]["id"] != "confluence:X:1#s0" || also[0]["url"] != "https://c" {
		t.Fatalf("copy not listed: %+v", got[0])
	}
	if _, ok := got[0]["canonical"]; ok {
		t.Fatal("canonical must not be returned")
	}
}
