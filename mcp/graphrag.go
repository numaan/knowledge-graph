package main

import (
	"context"
	"fmt"
)

// Shared parts of the GraphRAG tools global_search and local_search: the reader seam, community report
// retrieval and the visibility rules. Reports and members are visible only when the ACL of the caller
// allows them. A hidden node must not appear in any output (names, ids, counts of text).

const (
	graphragOutputChars  = 24000 // byte budget of one tool output
	graphragExcerptChars = 400   // longest excerpt of a summary in a result list
	reportFetch          = 40    // candidates per index, like Candidates; the ACL filter runs after the fetch
)

// graphReader is the part of Graph the GraphRAG code needs. Tests replace it.
type graphReader interface {
	Read(ctx context.Context, cypher string, params map[string]any) ([]map[string]any, error)
}

// reader returns the test override when set, else the graph.
func (s *Server) reader() graphReader {
	if s.graphOverride != nil {
		return s.graphOverride
	}
	return s.graph
}

// reportRow is one community report as the tools use it.
type reportRow struct {
	ID          string   `json:"id"`
	Title       string   `json:"title"`
	Summary     string   `json:"summary"`
	Findings    []string `json:"findings,omitempty"`
	Rank        float64  `json:"rank"`
	Level       int      `json:"level"`
	CommunityID string   `json:"community_id,omitempty"`
	acl         any
	repo        string
}

// memberRef is a visible member entity of a community.
type memberRef struct {
	ID   string `json:"id"`
	Name string `json:"name,omitempty"`
	Kind string `json:"kind,omitempty"`
}

// visibleScope applies the scope of a stored node: a non-empty acl list must pass allowed; an empty
// list with a repo means repo scope (the same rule as for code nodes); an empty list without a repo is
// public. An acl of an unknown type is hidden.
func (s *Server) visibleScope(ctx context.Context, acl any, repo string) bool {
	var list []any
	switch a := acl.(type) {
	case nil:
	case []any:
		list = a
	case []string:
		for _, g := range a {
			list = append(list, g)
		}
	default:
		return false
	}
	if len(list) > 0 {
		return s.allowed(ctx, list)
	}
	return s.allowedRepo(ctx, repo)
}

// visibleReport reports whether the caller may see the report.
func (s *Server) visibleReport(ctx context.Context, r reportRow) bool {
	return s.visibleScope(ctx, r.acl, r.repo)
}

func asString(v any) string { s, _ := v.(string); return s }

func asFloat(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case int64:
		return float64(x)
	case int:
		return float64(x)
	}
	return 0
}

func asStrings(v any) []string {
	var out []string
	switch x := v.(type) {
	case []string:
		out = append(out, x...)
	case []any:
		for _, e := range x {
			if s, ok := e.(string); ok {
				out = append(out, s)
			}
		}
	}
	return out
}

func toReport(r map[string]any) reportRow {
	return reportRow{ID: asString(r["id"]), Title: asString(r["title"]), Summary: asString(r["summary"]),
		Findings: asStrings(r["findings"]), Rank: asFloat(r["rank"]), Level: int(asFloat(r["level"])),
		CommunityID: asString(r["community_id"]), acl: r["acl"], repo: asString(r["repo"])}
}

// reportHits finds the reports for a question: vector and full text hits fused with RRF, then the
// level filter and the ACL filter, then the cut to k. Both indexes return their top hits before the
// filter, so each fetches reportFetch candidates and the cut comes last. level nil means every level.
func (s *Server) reportHits(ctx context.Context, question string, level *int, k int) ([]reportRow, error) {
	g := s.reader()
	p := map[string]any{"q": lucene(question), "level": nil}
	if level != nil {
		p["level"] = *level
	}
	const scope = `WHERE ($level IS NULL OR node.level = $level) AND ($repos IS NULL OR node.repo IS NULL OR node.repo IN $repos) `
	var byVector, byText []string
	var vecErr error
	if s.emb != nil {
		var vec []float64
		if vec, vecErr = s.emb.Query(ctx, question); vecErr == nil {
			p["v"] = vec
			var rows []map[string]any
			if rows, vecErr = g.Read(ctx, `CALL db.index.vector.queryNodes('community_report_embedding', 80, $v) YIELD node, score `+
				scope+`RETURN node.id AS id ORDER BY score DESC LIMIT 40`, p); vecErr == nil {
				byVector = ids(rows)
			}
		}
	}
	rows, textErr := g.Read(ctx, `CALL db.index.fulltext.queryNodes('community_report_text', $q, {limit: 80}) YIELD node, score `+
		scope+`RETURN node.id AS id ORDER BY score DESC LIMIT 40`, p)
	if textErr == nil {
		byText = ids(rows)
	}
	if textErr != nil && vecErr != nil {
		return nil, fmt.Errorf("report search: %w", textErr)
	}
	fused := rrf(byVector, byText)
	if len(fused) > 2*reportFetch {
		fused = fused[:2*reportFetch]
	}
	if len(fused) == 0 {
		return []reportRow{}, nil
	}
	idList := make([]string, len(fused))
	for i, f := range fused {
		idList[i] = f.ID
	}
	rows, err := g.Read(ctx, `UNWIND $ids AS id MATCH (r:CommunityReport {id: id})
		RETURN r.id AS id, r.title AS title, r.summary AS summary, r.findings AS findings, r.rank AS rank,
		       r.level AS level, r.acl AS acl, r.repo AS repo, r.community_id AS community_id`,
		map[string]any{"ids": idList})
	if err != nil {
		return nil, err
	}
	byID := make(map[string]reportRow, len(rows))
	for _, r := range rows {
		rr := toReport(r)
		byID[rr.ID] = rr
	}
	out := []reportRow{}
	for _, id := range idList {
		r, ok := byID[id]
		if !ok || (level != nil && r.Level != *level) || !s.visibleReport(ctx, r) {
			continue
		}
		out = append(out, r)
		if len(out) == k {
			break
		}
	}
	return out, nil
}

// reportMembers lists the member entities of a report, at most limit, and only those the caller may
// see. A report the caller cannot see has no visible members.
func (s *Server) reportMembers(ctx context.Context, reportID string, limit int) ([]memberRef, error) {
	rows, err := s.reader().Read(ctx, `MATCH (r:CommunityReport {id: $id})-[:REPORTS_ON]->(:Community)<-[:IN_COMMUNITY]-(m)
		OPTIONAL MATCH (m)-[:PART_OF]->(d:Document)
		RETURN m.id AS id, coalesce(m.qualname, m.name, m.title, m.key, m.path, m.id) AS name,
		       [l IN labels(m) WHERE NOT l IN ['Source', 'Code', 'Domain']][0] AS kind,
		       coalesce(m.acl, d.acl) AS acl, coalesce(m.repo, d.repo) AS repo,
		       r.acl AS report_acl, r.repo AS report_repo
		ORDER BY m.id LIMIT $fetch`,
		map[string]any{"id": reportID, "fetch": min(max(limit, 1)*5, 500)})
	if err != nil {
		return nil, err
	}
	out := []memberRef{}
	for _, r := range rows {
		if len(out) == limit {
			break
		}
		if !s.visibleScope(ctx, r["report_acl"], asString(r["report_repo"])) || !s.visibleScope(ctx, r["acl"], asString(r["repo"])) {
			continue
		}
		out = append(out, memberRef{ID: asString(r["id"]), Name: asString(r["name"]), Kind: asString(r["kind"])})
	}
	return out, nil
}

// trimToBudget keeps items in order while their summed size stays within budget. It sets truncated
// when it drops at least one item.
func trimToBudget[T any](items []T, budget int, size func(T) int) ([]T, bool) {
	used := 0
	for i, it := range items {
		used += size(it)
		if used > budget {
			return items[:i], true
		}
	}
	return items, false
}

// excerpt cuts text to n characters (not bytes) and marks the cut with an ellipsis.
func excerpt(text string, n int) string {
	r := []rune(text)
	if len(r) <= n {
		return text
	}
	return string(r[:n]) + "…"
}
