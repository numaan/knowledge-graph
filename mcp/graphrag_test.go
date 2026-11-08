package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// fakeGraph answers a query with the first rule whose substring the Cypher contains.
type fakeRule struct {
	sub  string
	rows func(p map[string]any) []map[string]any
}

type fakeGraph struct {
	mu    sync.Mutex
	rules []fakeRule
	seen  []string
	args  []map[string]any
}

func (f *fakeGraph) Read(_ context.Context, cypher string, params map[string]any) ([]map[string]any, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.seen = append(f.seen, cypher)
	f.args = append(f.args, params)
	for _, r := range f.rules {
		if strings.Contains(cypher, r.sub) {
			return r.rows(params), nil
		}
	}
	return []map[string]any{}, nil
}

func fixed(rows ...map[string]any) func(map[string]any) []map[string]any {
	return func(map[string]any) []map[string]any { return rows }
}

func idRows(ids ...string) []map[string]any {
	out := []map[string]any{}
	for _, id := range ids {
		out = append(out, map[string]any{"id": id})
	}
	return out
}

func report(id string, level int, acl []any, repo string) map[string]any {
	return map[string]any{"id": id, "title": "Title " + id, "summary": "Summary " + id, "findings": []any{"f1 " + id},
		"rank": 7.5, "level": int64(level), "acl": acl, "repo": repo, "community_id": "community:" + id}
}

// reportGraph returns a fake with the given vector and full text order and report rows by id.
func reportGraph(vector, text []string, reports ...map[string]any) *fakeGraph {
	byID := map[string]map[string]any{}
	for _, r := range reports {
		byID[r["id"].(string)] = r
	}
	return &fakeGraph{rules: []fakeRule{
		{"community_report_embedding", fixed(idRows(vector...)...)},
		{"community_report_text", fixed(idRows(text...)...)},
		{"MATCH (r:CommunityReport {id: id})", func(p map[string]any) []map[string]any {
			out := []map[string]any{}
			for _, id := range p["ids"].([]string) {
				if r, ok := byID[id]; ok {
					out = append(out, r)
				}
			}
			return out
		}},
	}}
}

func testEmbedder(t *testing.T) *Embedder {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"data": []any{map[string]any{"embedding": []float64{0.1, 0.2}}}})
	}))
	t.Cleanup(srv.Close)
	return NewEmbedder(srv.URL, "m", "")
}

func graphragServer(t *testing.T, g graphReader) *Server {
	t.Helper()
	return &Server{graphOverride: g, emb: testEmbedder(t), groups: map[string]bool{"*": true}}
}

func callerCtx(groups ...string) context.Context {
	m := map[string]bool{}
	for _, g := range groups {
		m[g] = true
	}
	return withAccess(context.Background(), &Access{User: "t", Groups: m, Via: "token"})
}

func reportIDs(rows []reportRow) []string {
	out := []string{}
	for _, r := range rows {
		out = append(out, r.ID)
	}
	return out
}

func equal(a, b []string) bool {
	return strings.Join(a, "|") == strings.Join(b, "|")
}

func TestReaderOverride(t *testing.T) {
	g := &fakeGraph{}
	if (&Server{graphOverride: g}).reader() != graphReader(g) {
		t.Fatal("reader() must return the override")
	}
}

func TestReportHitsACL(t *testing.T) {
	restricted := report("report:r", 0, []any{"jira:PREC"}, "")
	public := report("report:p", 0, []any{}, "")
	g := reportGraph([]string{"report:r", "report:p"}, []string{"report:p", "report:r"}, restricted, public)
	s := graphragServer(t, g)
	cases := []struct {
		name   string
		groups []string
		want   []string
	}{
		{"other group drops restricted", []string{"confluence:ENG"}, []string{"report:p"}},
		{"matching group keeps both", []string{"jira:PREC"}, []string{"report:p", "report:r"}},
		{"star keeps both", []string{"*"}, []string{"report:p", "report:r"}},
		{"no groups keeps public only", nil, []string{"report:p"}},
	}
	for _, c := range cases {
		got, err := s.reportHits(callerCtx(c.groups...), "question", nil, 8)
		if err != nil {
			t.Fatal(c.name, err)
		}
		if !equal(reportIDs(got), c.want) {
			t.Errorf("%s: got %v want %v", c.name, reportIDs(got), c.want)
		}
	}
}

func TestReportHitsRepoScope(t *testing.T) {
	mine := report("report:mine", 0, []any{}, "precept")
	other := report("report:other", 0, []any{}, "bookshop")
	g := reportGraph([]string{"report:other", "report:mine"}, []string{"report:mine"}, mine, other)
	s := graphragServer(t, g)
	got, _ := s.reportHits(callerCtx("repo:precept"), "q", nil, 8)
	if !equal(reportIDs(got), []string{"report:mine"}) {
		t.Fatalf("repo scope: %v", reportIDs(got))
	}
	got, _ = s.reportHits(callerCtx("*"), "q", nil, 8)
	if len(got) != 2 {
		t.Fatalf("star sees both repos: %v", reportIDs(got))
	}
	// the Cypher carries the repo scope too
	joined := strings.Join(g.seen, "\n")
	if !strings.Contains(joined, "$repos IS NULL") {
		t.Error("report queries must carry the $repos scope")
	}
}

func TestReportHitsLevel(t *testing.T) {
	l0 := report("report:l0", 0, []any{}, "")
	l1 := report("report:l1", 1, []any{}, "")
	g := reportGraph([]string{"report:l0", "report:l1"}, []string{"report:l1"}, l0, l1)
	s := graphragServer(t, g)
	one := 1
	got, _ := s.reportHits(callerCtx("*"), "q", &one, 8)
	if !equal(reportIDs(got), []string{"report:l1"}) {
		t.Fatalf("level filter: %v", reportIDs(got))
	}
	got, _ = s.reportHits(callerCtx("*"), "q", nil, 8)
	if len(got) != 2 {
		t.Fatalf("no level: %v", reportIDs(got))
	}
}

func TestReportHitsFusionOrderAndCut(t *testing.T) {
	a, b, c := report("report:a", 0, []any{}, ""), report("report:b", 0, []any{}, ""), report("report:c", 0, []any{}, "")
	// a is in both lists, c is first in vector only, b is second in text only: order a, c, b
	g := reportGraph([]string{"report:c", "report:a"}, []string{"report:a", "report:b"}, a, b, c)
	s := graphragServer(t, g)
	got, err := s.reportHits(callerCtx("*"), "q", nil, 8)
	if err != nil {
		t.Fatal(err)
	}
	if !equal(reportIDs(got), []string{"report:a", "report:c", "report:b"}) {
		t.Fatalf("fused order: %v", reportIDs(got))
	}
	got, _ = s.reportHits(callerCtx("*"), "q", nil, 2)
	if !equal(reportIDs(got), []string{"report:a", "report:c"}) {
		t.Fatalf("cut to k: %v", reportIDs(got))
	}
	if got[0].Title != "Title report:a" || got[0].Rank != 7.5 || got[0].Level != 0 || len(got[0].Findings) != 1 {
		t.Errorf("row fields: %+v", got[0])
	}
}

// A narrow caller must still get k reports when the top hits are hidden: the filter runs after the fetch.
func TestReportHitsFilterBeforeCut(t *testing.T) {
	var rows []map[string]any
	var order []string
	for _, id := range []string{"h1", "h2", "h3", "p1"} {
		acl := []any{"jira:PREC"}
		if id == "p1" {
			acl = []any{}
		}
		rows = append(rows, report("report:"+id, 0, acl, ""))
		order = append(order, "report:"+id)
	}
	s := graphragServer(t, reportGraph(order, nil, rows...))
	got, _ := s.reportHits(callerCtx("confluence:ENG"), "q", nil, 1)
	if !equal(reportIDs(got), []string{"report:p1"}) {
		t.Fatalf("got %v", reportIDs(got))
	}
}

func TestReportMembersOnlyVisible(t *testing.T) {
	member := func(id, name string, acl []any, repo string) map[string]any {
		return map[string]any{"id": id, "name": name, "kind": "Ticket", "acl": acl, "repo": repo,
			"report_acl": []any{}, "report_repo": ""}
	}
	g := &fakeGraph{rules: []fakeRule{{"REPORTS_ON", fixed(
		member("m:public", "Public member", []any{}, ""),
		member("m:hidden", "SECRET-NAME-91", []any{"jira:PREC"}, ""),
		member("m:othrepo", "OTHER-REPO-NAME", []any{}, "bookshop"),
		member("m:mine", "Mine", []any{}, "precept"),
	)}}}
	s := graphragServer(t, g)
	got, err := s.reportMembers(callerCtx("repo:precept"), "report:r", 10)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(got)
	out := string(b)
	for _, bad := range []string{"m:hidden", "SECRET-NAME-91", "m:othrepo", "OTHER-REPO-NAME"} {
		if strings.Contains(out, bad) {
			t.Errorf("hidden node leaked %q in %s", bad, out)
		}
	}
	if len(got) != 2 || got[0].ID != "m:public" || got[1].ID != "m:mine" {
		t.Fatalf("members: %s", out)
	}
	all, _ := s.reportMembers(callerCtx("*"), "report:r", 2)
	if len(all) != 2 {
		t.Errorf("limit: %d", len(all))
	}
}

func TestReportMembersHiddenReport(t *testing.T) {
	g := &fakeGraph{rules: []fakeRule{{"REPORTS_ON", fixed(map[string]any{"id": "m:1", "name": "N", "acl": []any{},
		"repo": "", "report_acl": []any{"jira:PREC"}, "report_repo": ""})}}}
	s := graphragServer(t, g)
	got, _ := s.reportMembers(callerCtx("confluence:ENG"), "report:r", 10)
	if len(got) != 0 {
		t.Fatalf("members of a hidden report: %v", got)
	}
}

func TestTrimToBudget(t *testing.T) {
	items := []string{strings.Repeat("a", 10000), strings.Repeat("b", 10000), strings.Repeat("c", 10000)}
	kept, truncated := trimToBudget(items, graphragOutputChars, func(s string) int { return len(s) })
	if len(kept) != 2 || !truncated {
		t.Fatalf("kept %d truncated %v", len(kept), truncated)
	}
	kept, truncated = trimToBudget(items[:2], graphragOutputChars, func(s string) int { return len(s) })
	if len(kept) != 2 || truncated {
		t.Fatalf("within budget: kept %d truncated %v", len(kept), truncated)
	}
	// an item that alone exceeds the budget is dropped; callers cut long text with excerpt first
	kept, truncated = trimToBudget([]string{strings.Repeat("x", 30000)}, graphragOutputChars, func(s string) int { return len(s) })
	if len(kept) != 0 || !truncated {
		t.Fatalf("one oversized item: kept %d truncated %v", len(kept), truncated)
	}
	if graphragOutputChars != 24000 || graphragExcerptChars != 400 {
		t.Error("constants")
	}
}

func TestExcerpt(t *testing.T) {
	if got := excerpt(strings.Repeat("é", 500), graphragExcerptChars); len([]rune(got)) > graphragExcerptChars+1 {
		t.Errorf("excerpt too long: %d", len([]rune(got)))
	}
	if excerpt("short", 400) != "short" {
		t.Error("short text must stay")
	}
}
