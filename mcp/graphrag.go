package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"
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

// ---- global_search ------------------------------------------------------------------------------

type GlobalSearchIn struct {
	Question string `json:"question" jsonschema:"broad question about the whole system or a large area, e.g. 'what are the main themes of the payment domain?'"`
	Level    *int   `json:"level,omitempty" jsonschema:"community level: 0 is the finest, higher levels are broader; empty searches all levels"`
	TopK     int    `json:"top_k,omitempty" jsonschema:"reports to read, 1-20 (default 8)"`
}

// GlobalReport is one ranked community report with its visible member entities.
type GlobalReport struct {
	ID       string      `json:"id"`
	Title    string      `json:"title"`
	Level    int         `json:"level"`
	Rank     float64     `json:"rank"`
	Summary  string      `json:"summary"`
	Entities []memberRef `json:"entities"`
}

// GlobalCitation points from a citation number in the answer to a report and its visible members.
type GlobalCitation struct {
	N        int         `json:"n"`
	ID       string      `json:"id"`
	Title    string      `json:"title"`
	Entities []memberRef `json:"entities"`
}

type GlobalSearchOut struct {
	Answer           string           `json:"answer,omitempty"`
	Reports          []GlobalReport   `json:"reports"`
	Citations        []GlobalCitation `json:"citations"`
	InvalidCitations []int            `json:"invalid_citations,omitempty"`
	Note             string           `json:"note,omitempty"`
	Truncated        bool             `json:"truncated,omitempty"`
}

const (
	globalMembers   = 10  // member entities listed per report
	globalNameChars = 200 // longest member name in the output
	globalMapTokens = 300
	globalRedTokens = 1000
	globalItemChars = 2500 // one report in a prompt
)

const globalMapSystem = "You read one community report about a software system. Answer the question with the facts of " +
	"this report only, in at most 80 words. When the report has nothing relevant, answer exactly NONE."

const globalReduceSystem = "You answer a broad question about a software system. Use only the numbered context items. " +
	"Each item is a partial answer from one community report. Put the number of the supporting item in square brackets " +
	"after each claim, for example [2] or [1][3]. Use only numbers that appear in the context. When the context does not " +
	"answer the question, say so in one sentence. Write at most 250 words."

func (s *Server) registerGraphRAG(m *mcp.Server) {
	mcp.AddTool(m, &mcp.Tool{Name: "global_search", Description: "Answer a broad question about the whole system or a large " +
		"area (themes, main parts, how areas relate) from community reports of the knowledge graph. Use it when no single " +
		"symbol or document answers the question. It is for agents that are not coding agents, such as analysis, planning " +
		"or support agents; coding agents use search_graph and impact_analysis. It returns ranked reports with member " +
		"entities and, when an LLM is configured, an answer with numbered citations to the reports."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in GlobalSearchIn) (*mcp.CallToolResult, GlobalSearchOut, error) {
			if strings.TrimSpace(in.Question) == "" {
				return nil, GlobalSearchOut{}, fmt.Errorf("question is empty")
			}
			out, err := s.globalSearch(ctx, in)
			return nil, out, err
		})
}

func (s *Server) globalSearch(ctx context.Context, in GlobalSearchIn) (GlobalSearchOut, error) {
	out := GlobalSearchOut{Reports: []GlobalReport{}, Citations: []GlobalCitation{}}
	k := clamp(in.TopK, 1, 20, 8)
	rows, err := s.reportHits(ctx, in.Question, in.Level, k)
	if err != nil {
		return out, err
	}
	if len(rows) == 0 {
		out.Answer = "No community report matches this question. Try other words, another level, or search_knowledge."
		out.Note = "no reports found; no LLM call"
		return out, nil
	}
	members := make([][]memberRef, len(rows))
	for i, r := range rows {
		ms, err := s.reportMembers(ctx, r.ID, globalMembers)
		if err != nil {
			log.Printf("global_search: members of %s: %v", r.ID, err)
			ms = []memberRef{}
		}
		for j := range ms {
			ms[j].Name = excerpt(ms[j].Name, globalNameChars)
		}
		members[i] = ms
	}
	var cited []citeRef
	if s.llm == nil {
		out.Note = "no LLM configured; ranked reports only"
	} else {
		cited = s.globalReduce(ctx, in.Question, rows, &out)
	}
	for i, r := range rows {
		out.Reports = append(out.Reports, GlobalReport{ID: r.ID, Title: r.Title, Level: r.Level, Rank: r.Rank,
			Summary: excerpt(r.Summary, graphragExcerptChars), Entities: members[i]})
	}
	for _, c := range cited {
		out.Citations = append(out.Citations, GlobalCitation{N: c.n, ID: rows[c.row].ID, Title: rows[c.row].Title, Entities: members[c.row]})
	}
	size := func(v any) int { b, _ := json.Marshal(v); return len(b) }
	budget := graphragOutputChars - len(out.Answer) - len(out.Note) - 400
	var cut bool
	out.Citations, cut = trimToBudget(out.Citations, budget/3, func(c GlobalCitation) int { return size(c) })
	out.Truncated = cut
	used := 0
	for _, c := range out.Citations {
		used += size(c)
	}
	out.Reports, cut = trimToBudget(out.Reports, budget-used, func(r GlobalReport) int { return size(r) })
	out.Truncated = out.Truncated || cut
	return out, nil
}

// citeRef links a citation number of the answer to an index in the report rows.
type citeRef struct{ n, row int }

// globalReduce runs one map call per report and one reduce call. It fills the answer, the invalid
// citation numbers and the note, and returns the valid citations. An
// LLM failure is not an error: the ranked reports still return, with a note.
func (s *Server) globalReduce(ctx context.Context, q string, rows []reportRow, out *GlobalSearchOut) []citeRef {
	var b strings.Builder
	fmt.Fprintf(&b, "Question: %s\n\nContext:\n", q)
	index := []int{} // context number -> index in rows
	for i, r := range rows {
		prompt := fmt.Sprintf("Question: %s\n\nReport: %s\n%s\n%s", q, r.Title, r.Summary, strings.Join(r.Findings, "\n"))
		part, err := s.llm.Complete(ctx, globalMapSystem, cutString(prompt, globalItemChars), globalMapTokens)
		if err != nil {
			log.Printf("global_search: map %s: %v", r.ID, err)
			out.Note = "an LLM call failed; ranked reports only"
			return nil
		}
		part = strings.TrimSpace(part)
		if part == "" || strings.EqualFold(strings.Trim(part, ". "), "NONE") {
			continue
		}
		index = append(index, i)
		fmt.Fprintf(&b, "[%d] %s (id: %s)\n%s\n\n", len(index), r.Title, r.ID, cutString(part, globalItemChars))
	}
	if len(index) == 0 {
		out.Answer = "No community report holds an answer to this question."
		out.Note = "no report was relevant; the reports below are the closest matches"
		return nil
	}
	text, err := s.llm.Complete(ctx, globalReduceSystem, b.String(), globalRedTokens)
	if err != nil {
		log.Printf("global_search: reduce: %v", err)
		out.Note = "an LLM call failed; ranked reports only"
		return nil
	}
	answer, valid, invalid := checkCitations(text, len(index))
	out.Answer, out.InvalidCitations = answer, invalid
	if len(invalid) > 0 {
		out.Note = fmt.Sprintf("removed %d citation numbers that were not in the context", len(invalid))
	}
	cited := make([]citeRef, 0, len(valid))
	for _, n := range valid {
		cited = append(cited, citeRef{n, index[n-1]})
	}
	return cited
}
