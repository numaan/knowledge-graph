package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"sort"
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
	if textErr != nil && (vecErr != nil || s.emb == nil) {
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
	s.registerLocalSearch(m)
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

// ---- local_search helpers --------------------------------------------------------------------------

const (
	localHopCap        = 15              // new neighbours taken from one node in one hop
	localNodeCap       = 60              // nodes in one result, seeds included
	localCommunityCap  = 5               // community report titles in one result
	localDefaultTokens = 4000            // default output budget, in tokens (chars / 4)
	localSeedCap       = 5               // seeds expanded per call
	localBatch         = 15              // nodes expanded per query: rows per query stay below the 500-row limit of Graph.Read
	localPerNode       = 2 * localHopCap // rows fetched per node, spare for hidden neighbours
)

// localTokens clamps a token budget to 500..16000; 0 means the default.
func localTokens(n int) int { return clamp(n, 500, 16000, localDefaultTokens) }

// localEdgeTypes are the relations local_search follows: code, domain and cross-source links. Chunks and
// community nodes are not entities and are left out.
var localEdgeTypes = []string{"CALLS", "IMPORTS", "DEFINES", "CONTAINS", "EXTENDS", "IMPLEMENTS", "USES_TYPE", "REALISES",
	"IMPLEMENTED_BY", "VALIDATED_BY", "COVERS", "REFERENCES", "LINKS", "IMPLEMENTED_IN", "DEPENDS_ON", "CALLS_HTTP", "TESTS",
	"PUBLISHES", "SUBSCRIBES", "DESCRIBES", "USES_SCHEMA", "MAPS_TO", "READS", "WRITES"}

// localNode is a visible entity in a local result.
type localNode struct {
	ID   string `json:"id"`
	Name string `json:"name,omitempty"`
	Kind string `json:"kind,omitempty"`
	Hop  int    `json:"hop"`
}

// localRel is a typed relation between two nodes that are both in the result.
type localRel struct {
	From string `json:"from"`
	To   string `json:"to"`
	Type string `json:"type"`
}

// localSubgraph is the expansion around the seeds. Truncated is set when a cap dropped a neighbour.
type localSubgraph struct {
	Nodes     []localNode `json:"nodes"`
	Rels      []localRel  `json:"relations"`
	Truncated bool        `json:"truncated,omitempty"`
}

// visibleLocal keeps the nodes of the rows the caller may see. Visibility is decided here, in Go, for
// every row: the scope of the node (acl, or the acl and repo of its document) must pass visibleScope.
// A node with any row the caller cannot see is dropped (fail closed).
func (s *Server) visibleLocal(ctx context.Context, rows []map[string]any) map[string]localNode {
	out := map[string]localNode{}
	hidden := map[string]bool{}
	for _, r := range rows {
		id := asString(r["id"])
		if id == "" {
			continue
		}
		if !s.visibleScope(ctx, r["acl"], asString(r["repo"])) {
			hidden[id] = true
			continue
		}
		if _, ok := out[id]; !ok {
			out[id] = localNode{ID: id, Name: excerpt(asString(r["name"]), globalNameChars), Kind: asString(r["kind"])}
		}
	}
	for id := range hidden {
		delete(out, id)
	}
	return out
}

const localNodeCols = `coalesce(n.qualname, n.name, n.title, n.key, n.path, n.id) AS name,
	[l IN labels(n) WHERE NOT l IN ['Source', 'Code', 'Domain']][0] AS kind, coalesce(n.acl, d.acl) AS acl, coalesce(n.repo, d.repo) AS repo`

// nodeByID returns a Cypher subquery that binds n to the node whose id is the variable v. Each branch
// uses a label with an id lookup (as tools.go does), so no branch scans all nodes. The subquery imports
// v and returns only n.
func nodeByID(v string) string {
	return strings.ReplaceAll(`CALL (@) { MATCH (n:Code {id: @}) RETURN n UNION MATCH (n:Domain {id: @}) RETURN n
		UNION MATCH (n:Requirement {id: @}) RETURN n UNION MATCH (n:Source {id: @}) RETURN n }`, "@", v)
}

// fetchNodes loads nodes by id and returns the visible ones.
func (s *Server) fetchNodes(ctx context.Context, idList []string) (map[string]localNode, error) {
	if len(idList) == 0 {
		return map[string]localNode{}, nil
	}
	rows, err := s.reader().Read(ctx, `UNWIND $ids AS nid `+nodeByID("nid")+`
			WITH n WHERE NOT n:Chunk AND NOT n:Community AND NOT n:CommunityReport AND ($repos IS NULL OR n.repo IS NULL OR n.repo IN $repos)
		OPTIONAL MATCH (n)-[:PART_OF]->(d:Document)
		RETURN n.id AS id, `+localNodeCols, map[string]any{"ids": idList})
	if err != nil {
		return nil, err
	}
	return s.visibleLocal(ctx, rows), nil
}

// localSeeds returns the ids of the visible nodes to expand from, at most localSeedCap. Given seeds are
// resolved as ids first, then as an exact qualified or short name that matches one visible node. Without
// seeds the question runs a hybrid search (chunks, tickets, sections, pull requests; vector and full
// text, fused with RRF). A seed that is missing and a seed that is hidden give the same error.
func (s *Server) localSeeds(ctx context.Context, question string, seeds []string) ([]string, error) {
	if len(seeds) > 0 {
		return s.explicitSeeds(ctx, seeds)
	}
	ids, err := s.hybridSeeds(ctx, question)
	if err != nil {
		return nil, err
	}
	nodes, err := s.fetchNodes(ctx, ids)
	if err != nil {
		return nil, err
	}
	out := []string{}
	for _, id := range ids {
		if _, ok := nodes[id]; ok {
			out = append(out, id)
			if len(out) == localSeedCap {
				break
			}
		}
	}
	return out, nil
}

func (s *Server) explicitSeeds(ctx context.Context, seeds []string) ([]string, error) {
	refs := []string{}
	seen := map[string]bool{}
	for _, r := range seeds {
		if r = strings.TrimSpace(r); r != "" && !seen[r] {
			seen[r] = true
			refs = append(refs, r)
		}
	}
	if len(refs) > localSeedCap {
		refs = refs[:localSeedCap]
	}
	byID, err := s.fetchNodes(ctx, refs)
	if err != nil {
		return nil, err
	}
	out := []string{}
	for _, ref := range refs {
		if _, ok := byID[ref]; ok {
			out = append(out, ref)
			continue
		}
		rows, err := s.reader().Read(ctx, `MATCH (n:Symbol) WHERE (n.qualname = $ref OR n.name = $ref) AND ($repos IS NULL OR n.repo IN $repos)
			OPTIONAL MATCH (n)-[:PART_OF]->(d:Document)
			RETURN n.id AS id, `+localNodeCols+` ORDER BY n.qualname = $ref DESC, n.path LIMIT 11`, map[string]any{"ref": ref})
		if err != nil {
			return nil, err
		}
		if found := s.visibleLocal(ctx, rows); len(found) == 1 {
			for id := range found {
				out = append(out, id)
			}
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("no visible node found for the seeds; use search_graph or find_symbol to find node ids")
	}
	return out, nil
}

// hybridSeeds ranks candidate node ids for a question. The caller still filters them by visibility.
func (s *Server) hybridSeeds(ctx context.Context, question string) ([]string, error) {
	g := s.reader()
	p := map[string]any{"q": lucene(question)}
	failed, queries := 0, 0
	run := func(cypher string) []string {
		queries++
		rows, err := g.Read(ctx, cypher, p)
		if err != nil {
			failed++
			return nil
		}
		return ids(rows)
	}
	const chunkScope = `WHERE ($repos IS NULL OR node.repo IS NULL OR node.repo IN $repos) RETURN node.id AS id ORDER BY score DESC LIMIT 40`
	const srcScope = `WHERE (node:Ticket OR node:Section OR node:PullRequest) RETURN node.id AS id ORDER BY score DESC LIMIT 30`
	var chunkVec, srcVec []string
	if s.emb != nil {
		if vec, err := s.emb.Query(ctx, question); err == nil {
			p["v"] = vec
			chunkVec = run(`CALL db.index.vector.queryNodes('chunk_embedding', 80, $v) YIELD node, score ` + chunkScope)
			srcVec = run(`CALL db.index.vector.queryNodes('source_embedding', 60, $v) YIELD node, score ` + srcScope)
		}
	}
	chunkText := run(`CALL db.index.fulltext.queryNodes('chunk_text', $q, {limit: 80}) YIELD node, score ` + chunkScope)
	srcText := run(`CALL db.index.fulltext.queryNodes('source_text', $q, {limit: 60}) YIELD node, score ` + srcScope)
	if failed == queries {
		return nil, fmt.Errorf("seed search failed")
	}
	// chunks stand for the symbol or file they belong to
	chunks := rrf(chunkVec, chunkText)
	if len(chunks) > reportFetch {
		chunks = chunks[:reportFetch]
	}
	var owners []string
	if len(chunks) > 0 {
		chunkIDs := make([]string, len(chunks))
		for i, c := range chunks {
			chunkIDs[i] = c.ID
		}
		rows, err := g.Read(ctx, `UNWIND $ids AS id MATCH (c:Chunk {id: id})-[:PART_OF]->(o) RETURN c.id AS id, o.id AS owner`,
			map[string]any{"ids": chunkIDs})
		if err != nil {
			return nil, err
		}
		ownerOf := map[string]string{}
		for _, r := range rows {
			ownerOf[asString(r["id"])] = asString(r["owner"])
		}
		seen := map[string]bool{}
		for _, id := range chunkIDs {
			if o := ownerOf[id]; o != "" && !seen[o] {
				seen[o] = true
				owners = append(owners, o)
			}
		}
	}
	fused := rrf(owners, srcVec, srcText)
	if len(fused) > 3*localSeedCap {
		fused = fused[:3*localSeedCap]
	}
	out := make([]string, len(fused))
	for i, f := range fused {
		out[i] = f.ID
	}
	return out, nil
}

// expandLocal walks the graph from the seeds for hops steps (1..3, default 2). It takes at most
// localHopCap new neighbours from each node in each hop and stops at localNodeCap nodes. Every seed and
// every neighbour is checked with visibleScope; a hidden node is skipped, so it cannot be reached and
// no relation to it is returned. A relation is returned only when both of its nodes are in the result.
func (s *Server) expandLocal(ctx context.Context, seeds []string, hops int) (localSubgraph, error) {
	g := localSubgraph{Nodes: []localNode{}, Rels: []localRel{}}
	hops = clamp(hops, 1, 3, 2)
	known, err := s.fetchNodes(ctx, seeds)
	if err != nil {
		return g, err
	}
	index := map[string]bool{}
	var frontier []string
	for _, id := range seeds {
		if n, ok := known[id]; ok && !index[id] && len(g.Nodes) < localNodeCap {
			index[id] = true
			g.Nodes = append(g.Nodes, n)
			frontier = append(frontier, id)
		}
	}
	relSeen := map[localRel]bool{}
	for hop := 1; hop <= hops && len(frontier) > 0; hop++ {
		var rows []map[string]any
		for i := 0; i < len(frontier); i += localBatch {
			part, err := s.reader().Read(ctx, `UNWIND $ids AS seed
				CALL (seed) {
				  `+nodeByID("seed")+`
				  MATCH (n)-[r]-(b)
				  WHERE type(r) IN $types AND NOT b:Chunk AND NOT b:Community AND NOT b:CommunityReport
				    AND ($repos IS NULL OR b.repo IS NULL OR b.repo IN $repos)
				  OPTIONAL MATCH (b)-[:PART_OF]->(d:Document)
				  RETURN n.id AS a, b.id AS id, coalesce(b.qualname, b.name, b.title, b.key, b.path, b.id) AS name,
				         [l IN labels(b) WHERE NOT l IN ['Source', 'Code', 'Domain']][0] AS kind,
				         coalesce(b.acl, d.acl) AS acl, coalesce(b.repo, d.repo) AS repo,
				         type(r) AS type, startNode(r) = n AS out
				  ORDER BY b.id LIMIT $per
				}
				RETURN a, id, name, kind, acl, repo, type, out`,
				map[string]any{"ids": frontier[i:min(i+localBatch, len(frontier))], "types": localEdgeTypes, "per": localPerNode})
			if err != nil {
				return g, err
			}
			rows = append(rows, part...)
		}
		visible := s.visibleLocal(ctx, rows)
		byA := map[string][]map[string]any{}
		for _, r := range rows {
			byA[asString(r["a"])] = append(byA[asString(r["a"])], r)
		}
		var next []string
		for _, a := range frontier {
			added := 0
			for _, r := range byA[a] {
				id := asString(r["id"])
				n, ok := visible[id]
				if !ok || id == a {
					continue
				}
				if !index[id] {
					if added == localHopCap || len(g.Nodes) == localNodeCap {
						g.Truncated = true
						continue
					}
					added++
					n.Hop = hop
					index[id] = true
					g.Nodes = append(g.Nodes, n)
					next = append(next, id)
				}
				rel := localRel{From: a, To: id, Type: asString(r["type"])}
				if out, isBool := r["out"].(bool); isBool && !out {
					rel.From, rel.To = id, a
				}
				if !relSeen[rel] {
					relSeen[rel] = true
					g.Rels = append(g.Rels, rel)
				}
			}
		}
		frontier = next
	}
	return g, nil
}

// ---- local_search -------------------------------------------------------------------------------------

type LocalSearchIn struct {
	Question    string   `json:"question,omitempty" jsonschema:"question about an entity or a small area; used to find seeds when none are given"`
	Seeds       []string `json:"seeds,omitempty" jsonschema:"node ids or exact names to start from (max 5); empty finds seeds from the question"`
	Hops        int      `json:"hops,omitempty" jsonschema:"relation steps from the seeds, 1-3 (default 2)"`
	TokenBudget int      `json:"token_budget,omitempty" jsonschema:"output size in tokens (chars / 4), 500-16000 (default 4000)"`
}

// Every item carries a citation id (E, R, S or C and a number) so an agent can cite it.
type LocalEntity struct {
	Cite string `json:"cite"`
	ID   string `json:"id"`
	Name string `json:"name,omitempty"`
	Kind string `json:"kind,omitempty"`
	Hop  int    `json:"hop"`
}

type LocalRelation struct {
	Cite string `json:"cite"`
	From string `json:"from"`
	To   string `json:"to"`
	Type string `json:"type"`
}

type LocalSnippet struct {
	Cite   string `json:"cite"`
	ID     string `json:"id"`
	Entity string `json:"entity"`
	Text   string `json:"text"`
}

type LocalCommunity struct {
	Cite  string `json:"cite"`
	ID    string `json:"id"`
	Title string `json:"title"`
	Level int    `json:"level"`
}

type LocalSearchOut struct {
	Entities    []LocalEntity    `json:"entities"`
	Relations   []LocalRelation  `json:"relations"`
	Snippets    []LocalSnippet   `json:"snippets"`
	Communities []LocalCommunity `json:"communities"`
	Truncated   bool             `json:"truncated,omitempty"`
	Omitted     map[string]int   `json:"omitted,omitempty"`
	Note        string           `json:"note,omitempty"`
}

const localSnippetPerNode = 2 // chunks read per entity

func (s *Server) registerLocalSearch(m *mcp.Server) {
	mcp.AddTool(m, &mcp.Tool{Name: "local_search", Description: "Collect the context around one entity or a small area: " +
		"the neighbours of the seeds (typed relations, up to 3 hops), text snippets of those entities and the titles of the " +
		"communities they belong to. Use it to understand one named thing in depth. It is for agents that are not coding " +
		"agents, such as analysis, planning or support agents; coding agents use search_graph and impact_analysis. It needs " +
		"no LLM. Every item has a citation id. The output is cut to token_budget and the counts of cut items are in omitted."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in LocalSearchIn) (*mcp.CallToolResult, LocalSearchOut, error) {
			out, err := s.localSearch(ctx, in)
			return nil, out, err
		})
}

func (s *Server) localSearch(ctx context.Context, in LocalSearchIn) (LocalSearchOut, error) {
	out := LocalSearchOut{Entities: []LocalEntity{}, Relations: []LocalRelation{}, Snippets: []LocalSnippet{}, Communities: []LocalCommunity{}}
	if strings.TrimSpace(in.Question) == "" && len(in.Seeds) == 0 {
		return out, fmt.Errorf("give a question or seeds")
	}
	seeds, err := s.localSeeds(ctx, in.Question, in.Seeds)
	if err != nil {
		return out, err
	}
	if len(seeds) == 0 {
		out.Note = "No matching entity found. Try other words, or give node ids as seeds (see search_graph)."
		return out, nil
	}
	sub, err := s.expandLocal(ctx, seeds, in.Hops)
	if err != nil {
		return out, err
	}
	nodeIDs := make([]string, len(sub.Nodes))
	for i, n := range sub.Nodes {
		nodeIDs[i] = n.ID
	}
	snippets, err := s.localSnippets(ctx, nodeIDs)
	if err != nil {
		log.Printf("local_search: snippets: %v", err)
	}
	comms, err := s.localCommunities(ctx, nodeIDs)
	if err != nil {
		log.Printf("local_search: communities: %v", err)
	}
	for i, n := range sub.Nodes {
		out.Entities = append(out.Entities, LocalEntity{Cite: fmt.Sprintf("E%d", i+1), ID: n.ID, Name: n.Name, Kind: n.Kind, Hop: n.Hop})
	}
	for i, r := range sub.Rels {
		out.Relations = append(out.Relations, LocalRelation{Cite: fmt.Sprintf("R%d", i+1), From: r.From, To: r.To, Type: r.Type})
	}
	for i, sn := range snippets {
		sn.Cite = fmt.Sprintf("S%d", i+1)
		out.Snippets = append(out.Snippets, sn)
	}
	for i, c := range comms {
		c.Cite = fmt.Sprintf("C%d", i+1)
		out.Communities = append(out.Communities, c)
	}
	out.Truncated = sub.Truncated
	trimLocal(&out, localTokens(in.TokenBudget)*4)
	return out, nil
}

// trimLocal cuts the lists to the budget in characters. Order of priority: entities, relations,
// communities, snippets. A relation whose entity was cut is dropped too.
func trimLocal(out *LocalSearchOut, budget int) {
	size := func(v any) int { b, _ := json.Marshal(v); return len(b) }
	left := budget - 300 // fixed fields of the envelope
	omit := map[string]int{}
	cut := func(name string, dropped int) {
		if dropped > 0 {
			omit[name] += dropped
			out.Truncated = true
		}
	}
	n := len(out.Entities)
	out.Entities, _ = trimToBudget(out.Entities, left, func(e LocalEntity) int { return size(e) })
	cut("entities", n-len(out.Entities))
	kept := map[string]bool{}
	for _, e := range out.Entities {
		left -= size(e)
		kept[e.ID] = true
	}
	n = len(out.Relations)
	both := []LocalRelation{}
	for _, r := range out.Relations {
		if kept[r.From] && kept[r.To] {
			both = append(both, r)
		}
	}
	out.Relations, _ = trimToBudget(both, left, func(r LocalRelation) int { return size(r) })
	cut("relations", n-len(out.Relations))
	for _, r := range out.Relations {
		left -= size(r)
	}
	n = len(out.Communities)
	out.Communities, _ = trimToBudget(out.Communities, left, func(c LocalCommunity) int { return size(c) })
	cut("communities", n-len(out.Communities))
	for _, c := range out.Communities {
		left -= size(c)
	}
	n = len(out.Snippets)
	out.Snippets, _ = trimToBudget(out.Snippets, left, func(sn LocalSnippet) int { return size(sn) })
	cut("snippets", n-len(out.Snippets))
	if len(omit) > 0 {
		out.Omitted = omit
	}
}

// localSnippets reads the text of the given nodes, at most localSnippetPerNode chunks each. The ids are
// the ids of visible nodes only; the text is the masked text when it exists.
func (s *Server) localSnippets(ctx context.Context, idList []string) ([]LocalSnippet, error) {
	out := []LocalSnippet{}
	for i := 0; i < len(idList); i += localBatch {
		rows, err := s.reader().Read(ctx, `UNWIND $ids AS nid `+nodeByID("nid")+`
			OPTIONAL MATCH (c:Chunk)-[:PART_OF]->(n)
			WITH n, c ORDER BY c.id
			WITH n, collect(c)[..`+fmt.Sprint(localSnippetPerNode)+`] AS cs
			UNWIND (CASE WHEN size(cs) = 0 THEN [null] ELSE cs END) AS c
			RETURN n.id AS node, coalesce(c.id, n.id) AS id, coalesce(c.text_masked, c.text, n.text_masked, n.text) AS text`,
			map[string]any{"ids": idList[i:min(i+localBatch, len(idList))]})
		if err != nil {
			return out, err
		}
		for _, r := range rows {
			if t := strings.TrimSpace(asString(r["text"])); t != "" {
				out = append(out, LocalSnippet{ID: asString(r["id"]), Entity: asString(r["node"]), Text: excerpt(t, graphragExcerptChars)})
			}
		}
	}
	return out, nil
}

// localCommunities returns the visible community reports of the given nodes: the reports that cover most
// of the nodes first, then by rank. At most localCommunityCap.
func (s *Server) localCommunities(ctx context.Context, idList []string) ([]LocalCommunity, error) {
	count := map[string]int{}
	byID := map[string]reportRow{}
	var firstErr error
	for i := 0; i < len(idList); i += localBatch {
		rows, err := s.reader().Read(ctx, `UNWIND $ids AS nid `+nodeByID("nid")+`
			MATCH (n)-[:IN_COMMUNITY]->(:Community)<-[:REPORTS_ON]-(r:CommunityReport)
			RETURN n.id AS node, r.id AS id, r.title AS title, r.summary AS summary, r.rank AS rank, r.level AS level,
			       r.acl AS acl, r.repo AS repo, r.community_id AS community_id`,
			map[string]any{"ids": idList[i:min(i+localBatch, len(idList))]})
		if err != nil {
			firstErr = err
			break
		}
		for _, row := range rows {
			r := toReport(row)
			if _, seen := byID[r.ID]; !seen {
				if !s.visibleReport(ctx, r) {
					continue
				}
				byID[r.ID] = r
			}
			count[r.ID]++
		}
	}
	reps := make([]reportRow, 0, len(byID))
	for _, r := range byID {
		reps = append(reps, r)
	}
	sort.Slice(reps, func(i, j int) bool {
		a, b := reps[i], reps[j]
		if count[a.ID] != count[b.ID] {
			return count[a.ID] > count[b.ID]
		}
		if a.Rank != b.Rank {
			return a.Rank > b.Rank
		}
		if a.Level != b.Level {
			return a.Level < b.Level
		}
		return a.ID < b.ID
	})
	if len(reps) > localCommunityCap {
		reps = reps[:localCommunityCap]
	}
	out := make([]LocalCommunity, len(reps))
	for i, r := range reps {
		out[i] = LocalCommunity{ID: r.ID, Title: excerpt(r.Title, globalNameChars), Level: r.Level}
	}
	return out, firstErr
}
