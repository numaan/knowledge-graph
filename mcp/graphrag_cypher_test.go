package main

import (
	"context"
	"errors"
	"os"
	"regexp"
	"strings"
	"testing"
)

// rawStrings returns the raw (backtick) string literals of a Go source file.
func rawStrings(src string) []string {
	var out []string
	for {
		i := strings.IndexByte(src, '`')
		if i < 0 {
			return out
		}
		j := strings.IndexByte(src[i+1:], '`')
		if j < 0 {
			return out
		}
		out = append(out, src[i+1:i+1+j])
		src = src[i+j+2:]
	}
}

// subqueries returns, for each `CALL (vars) {` in the Cypher, the imported variables and the body.
func subqueries(cypher string) (imports [][]string, bodies []string) {
	re := regexp.MustCompile(`CALL \(([^)]*)\) \{`)
	for _, m := range re.FindAllStringSubmatchIndex(cypher, -1) {
		depth, end := 1, -1
		for k := m[1]; k < len(cypher) && end < 0; k++ {
			switch cypher[k] {
			case '{':
				depth++
			case '}':
				if depth--; depth == 0 {
					end = k
				}
			}
		}
		if end < 0 {
			end = len(cypher)
		}
		var vars []string
		for _, v := range strings.Split(cypher[m[2]:m[3]], ",") {
			if v = strings.TrimSpace(v); v != "" {
				vars = append(vars, v)
			}
		}
		imports = append(imports, vars)
		bodies = append(bodies, cypher[m[1]:end])
	}
	return
}

// TestGraphragCypherStatic guards two classes of Cypher bug that the fake graph cannot see: a subquery
// that returns a name it imports, and an id lookup without a label (a scan of all nodes).
func TestGraphragCypherStatic(t *testing.T) {
	data, err := os.ReadFile("graphrag.go")
	if err != nil {
		t.Fatal(err)
	}
	unlabelled := []*regexp.Regexp{
		regexp.MustCompile(`MATCH \(\w+ ?\{\s*id\s*:`),
		regexp.MustCompile(`MATCH \(\w*\) WHERE \w+\.id\s*(=|IN)`),
	}
	// strings built at run time are checked through the builder
	queries := append(rawStrings(string(data)), nodeByID("x"), nodeByID("seed"))
	subs := 0
	for _, q := range queries {
		for _, re := range unlabelled {
			if m := re.FindString(q); m != "" {
				t.Errorf("unlabelled id lookup %q in %q", m, q)
			}
		}
		imports, bodies := subqueries(q)
		for i, vars := range imports {
			subs++
			for _, v := range vars {
				if regexp.MustCompile(`\bAS ` + regexp.QuoteMeta(v) + `\b`).MatchString(bodies[i]) {
					t.Errorf("subquery imports %q and returns a column of the same name: %q", v, bodies[i])
				}
			}
		}
	}
	if subs < 2 {
		t.Fatalf("the scan found %d subqueries; the check is broken", subs)
	}
}

func TestStaticChecksDetectBugs(t *testing.T) {
	_, bodies := subqueries(`UNWIND $ids AS id CALL (id) { MATCH (n:Code {id: id}) RETURN n.id AS id }`)
	if len(bodies) != 1 || !strings.Contains(bodies[0], "AS id") {
		t.Fatalf("subquery scan: %v", bodies)
	}
	if !regexp.MustCompile(`MATCH \(\w+ ?\{\s*id\s*:`).MatchString("MATCH (n {id: x})") {
		t.Fatal("unlabelled pattern does not match")
	}
}

func TestNodeByIDLabelled(t *testing.T) {
	q := nodeByID("seed")
	for _, want := range []string{"CALL (seed)", "(n:Code {id: seed})", "(n:Source {id: seed})", "(n:Domain {id: seed})", "(n:Requirement {id: seed})"} {
		if !strings.Contains(q, want) {
			t.Errorf("missing %q in %q", want, q)
		}
	}
}

// errGraph fails every query.
type errGraph struct{}

func (errGraph) Read(context.Context, string, map[string]any) ([]map[string]any, error) {
	return nil, errors.New("index missing")
}

func TestReportHitsTextErrorWithoutEmbedder(t *testing.T) {
	s := &Server{graphOverride: errGraph{}, groups: map[string]bool{"*": true}}
	if _, err := s.reportHits(context.Background(), "q", nil, 8); err == nil {
		t.Fatal("a failed text query with no vector query must return the error")
	}
}

func TestChunkScopeRepoLessSeeds(t *testing.T) {
	lf := newLocalFake([]map[string]any{nodeRow("doc", nil, ""), nodeRow("hid", []any{"jira:PREC"}, "")}, nil)
	lf.rules = append([]fakeRule{
		{"chunk_text", fixed(idRows("c1", "c2")...)},
		{"MATCH (c:Chunk {id: id})-[:PART_OF]->(o)", func(p map[string]any) []map[string]any {
			m := map[string]string{"c1": "doc", "c2": "hid"}
			out := []map[string]any{}
			for _, id := range p["ids"].([]string) {
				out = append(out, map[string]any{"id": id, "owner": m[id]})
			}
			return out
		}},
	}, lf.rules...)
	s := graphragServer(t, lf)
	got, err := s.localSeeds(callerCtx("repo:precept"), "how", nil)
	if err != nil || !equal(got, []string{"doc"}) {
		t.Fatalf("a repo-less chunk must seed and a hidden one must drop: %v %v", got, err)
	}
	found := false
	for _, q := range lf.seen {
		if strings.Contains(q, "chunk_text") {
			found = true
			if !strings.Contains(q, "node.repo IS NULL OR node.repo IN $repos") {
				t.Errorf("chunk scope must admit repo-less chunks: %q", q)
			}
		}
	}
	if !found {
		t.Fatal("no chunk query ran")
	}
}
