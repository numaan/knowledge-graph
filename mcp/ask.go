package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// ask: question -> router -> retrieval with the existing tools -> LLM answer with numbered
// citations. Every citation must point to an item of the retrieved context; other numbers are
// removed from the answer and reported in invalid_citations. Retrieval goes through the same
// $repos scope and ACL checks as the other tools, so ask cannot see more than they can.

type AskIn struct {
	Question string `json:"question" jsonschema:"question in natural language, e.g. 'why are scenarios not kept in local storage?'"`
	Repo     string `json:"repo,omitempty" jsonschema:"repository name; empty uses the default or searches all"`
	K        int    `json:"k,omitempty" jsonschema:"results per retrieval tool, 1-10 (default 6)"`
}

type Citation struct {
	N       int    `json:"n"`
	ID      string `json:"id"`
	Label   string `json:"label"`
	Title   string `json:"title"`
	Path    string `json:"path,omitempty"`
	URL     string `json:"url,omitempty"`
	Excerpt string `json:"excerpt"`
}

type AskOut struct {
	Answer           string     `json:"answer"`
	Citations        []Citation `json:"citations"`
	Confidence       float64    `json:"confidence"`
	ToolsUsed        []string   `json:"tools_used"`
	Route            Route      `json:"route"`
	InvalidCitations []int      `json:"invalid_citations,omitempty"`
	Note             string     `json:"note,omitempty"`
}

// evidence is one numbered context item.
type evidence struct {
	ID, Label, Title, Path, URL string
	Text                        string // what the LLM reads
}

const (
	askContextChars = 24000 // all context items together
	askItemChars    = 2500  // one context item
	askExcerptChars = 300
	askMaxTokens    = 1200
)

func (s *Server) registerAsk(m *mcp.Server) {
	if s.llm == nil {
		return // off when no LLM is configured (LLM_BASE_URL, LLM_MODEL)
	}
	mcp.AddTool(m, &mcp.Tool{Name: "ask", Description: "Answer a question about the system in prose, with numbered " +
		"citations to graph nodes (code, tickets, document sections, commits, requirements). It routes the question " +
		"(code location, impact, history, ticket, requirement, overview, people), runs the matching retrieval tools and " +
		"asks an LLM. Use it for a first answer; check the cited nodes with the other tools before you change code."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in AskIn) (*mcp.CallToolResult, AskOut, error) {
			if strings.TrimSpace(in.Question) == "" {
				return nil, AskOut{}, fmt.Errorf("question is empty")
			}
			route := routeQuestion(ctx, s.llm, os.Getenv("KG_ROUTER_LLM") == "on", in.Question)
			items, used, notes := s.gather(ctx, route, in.Question, s.repo(in.Repo), clamp(in.K, 1, 10, 6))
			out, err := answer(ctx, s.llm, in.Question, route, items)
			if err != nil {
				return nil, AskOut{}, err
			}
			out.ToolsUsed = used
			out.Note = strings.Join(append(notes, out.Note), "; ")
			out.Note = strings.Trim(out.Note, "; ")
			return nil, out, nil
		})
}

// ---- retrieval ----------------------------------------------------------------------------------

type gatherer struct {
	items []evidence
	seen  map[string]bool
}

func (g *gatherer) add(e evidence) {
	key := e.Label + "|" + e.ID
	if e.ID == "" || g.seen[key] {
		return
	}
	g.seen[key] = true
	g.items = append(g.items, e)
}

// gather runs the tools of the route. A failing tool is skipped with a note: optional layers
// (summaries, history, documents) may not exist in every graph.
func (s *Server) gather(ctx context.Context, r Route, q, repo string, k int) ([]evidence, []string, []string) {
	g := &gatherer{seen: map[string]bool{}}
	var used, notes []string
	var symbols []string // symbol ids for impact and history: named in the question first, then search hits
	for _, name := range r.Symbols {
		if id, err := s.resolve(ctx, name, repo); err == nil {
			symbols = append(symbols, id)
		}
	}
	run := func(tool string, f func() error) {
		before := len(g.items)
		if err := f(); err != nil {
			log.Printf("ask: %s: %v", tool, err)
			notes = append(notes, tool+" skipped")
			return
		}
		if len(g.items) > before && !slices.Contains(used, tool) {
			used = append(used, tool)
		}
	}
	for _, tool := range r.Tools {
		switch tool {
		case "search_graph":
			run(tool, func() error {
				ids, err := s.askCode(ctx, g, q, repo, k)
				symbols = append(symbols, ids...)
				return err
			})
		case "search_knowledge":
			kind := ""
			if r.Intent == IntentTicket {
				kind = "Ticket"
			}
			run(tool, func() error { return s.askKnowledge(ctx, g, q, kind, k) })
		case "get_change_history":
			run(tool, func() error { return s.askHistory(ctx, g, unique(symbols), k) })
		case "impact_analysis":
			run(tool, func() error { return s.askImpact(ctx, g, unique(symbols)) })
		case "get_context_for_ticket":
			run(tool, func() error { return s.askTickets(ctx, g, r.Tickets) })
		case "trace_requirement":
			run(tool, func() error { return s.askRequirements(ctx, g, r.Requirements, q, repo) })
		case "summaries":
			run(tool, func() error { return s.askSummaries(ctx, g, q, repo, k) })
		case "domain_overview":
			run(tool, func() error { return s.askConcepts(ctx, g, q, repo, k) })
		}
	}
	return g.items, used, notes
}

func str(v any) string {
	if v == nil {
		return ""
	}
	return fmt.Sprint(v)
}

func list(v any) string {
	xs, _ := v.([]any)
	parts := make([]string, 0, len(xs))
	for _, x := range xs {
		if x != nil && str(x) != "" {
			parts = append(parts, str(x))
		}
	}
	return strings.Join(parts, "; ")
}

// askCode is search_graph; it returns the symbol ids of the hits.
func (s *Server) askCode(ctx context.Context, g *gatherer, q, repo string, k int) ([]string, error) {
	hits, err := s.Candidates(ctx, repo, q, k)
	if err != nil {
		return nil, err
	}
	chunkIDs := make([]string, len(hits))
	for i, h := range hits {
		chunkIDs[i] = h.ID
	}
	rows, err := s.Expand(ctx, chunkIDs, askItemChars)
	if err != nil {
		return nil, err
	}
	var symbols []string
	for _, r := range rows {
		id := str(r["symbol_id"])
		symbols = append(symbols, id)
		text := str(r["code"])
		for _, f := range []string{"concepts", "rules", "callers", "callees", "tests"} {
			if l := list(r[f]); l != "" {
				text += "\n" + f + ": " + l
			}
		}
		g.add(evidence{ID: id, Label: str(r["kind"]), Title: str(r["symbol"]),
			Path: fmt.Sprintf("%s:%v-%v", str(r["path"]), r["start_line"], r["end_line"]), Text: text})
	}
	return symbols, nil
}

func (s *Server) askKnowledge(ctx context.Context, g *gatherer, q, kind string, k int) error {
	rows, err := s.Knowledge(ctx, q, kind, k)
	for _, r := range rows {
		title := str(r["title"])
		if key := str(r["key"]); key != "" {
			title = key + ": " + title
		}
		meta := strings.TrimSpace(str(r["type"]) + " " + str(r["status"]))
		text := str(r["text"])
		if meta != "" {
			text = meta + "\n" + text
		}
		g.add(evidence{ID: str(r["id"]), Label: str(r["kind"]), Title: title, URL: str(r["url"]), Text: text})
	}
	return err
}

// fileOf maps a symbol id <repo>:<path>#<name> to its file id <repo>:<path>.
func fileOf(id string) string {
	if i := strings.Index(id, "#"); i > 0 {
		return id[:i]
	}
	return id
}

// Node text that leaves the server (LLM prompt, citation excerpts) reads coalesce(x.text_masked, x.text):
// the pipeline (pipeline/kg_pipeline/privacy.py) stores a PII-masked copy in text_masked, as
// search_knowledge and get_context_for_ticket already use.
const (
	askHistoryCypher = `MATCH (c:Commit)-[:MODIFIES]->(x:Code) WHERE x.id IN $ids AND ($repos IS NULL OR c.repo IN $repos)
		WITH DISTINCT c OPTIONAL MATCH (c)-[:REFERENCES]->(t:Ticket)
		RETURN c.id AS id, c.sha AS sha, c.date AS date, c.author AS author, c.title AS title,
		       left(coalesce(c.text_masked, c.text), 800) AS message,
		       c.repo AS repo, c.acl AS acl, collect(t.key + ': ' + t.title) AS tickets
		ORDER BY date DESC LIMIT $limit`

	askTicketCypher = `MATCH (t:Ticket {id: $id})
		OPTIONAL MATCH (t)-[:CHILD_OF]->(p:Ticket)
		OPTIONAL MATCH (c:Commit)-[:REFERENCES]->(t) WHERE $repos IS NULL OR c.repo IN $repos
		WITH t, p, c ORDER BY c.date DESC
		RETURN t.id AS id, t.key AS key, t.title AS title, t.type AS type, t.status AS status, t.url AS url,
		       left(coalesce(t.text_masked, t.text), 3000) AS text, t.acl AS acl, p.key + ': ' + p.title AS parent,
		       collect(CASE WHEN c IS NULL THEN NULL ELSE {id: c.id, title: c.title, author: c.author, date: c.date, acl: c.acl} END)[..8] AS commits`

	askSummaryCypher = `CALL db.index.vector.queryNodes('summary_embedding', $k, $v) YIELD node, score
		WHERE ($repo = '' OR node.repo IS NULL OR node.repo = $repo) AND ($repos IS NULL OR node.repo IS NULL OR node.repo IN $repos)
		RETURN node.id AS id, node.level AS level, coalesce(node.title, node.path, node.name, node.id) AS title, node.path AS path,
		       node.url AS url, left(coalesce(node.text_masked, node.text), $chars) AS text, node.acl AS acl ORDER BY score DESC`
)

func (s *Server) askHistory(ctx context.Context, g *gatherer, symbols []string, k int) error {
	if len(symbols) == 0 {
		return nil
	}
	symbols = symbols[:min(len(symbols), 3)]
	targets := slices.Clone(symbols)
	for _, id := range symbols {
		targets = append(targets, fileOf(id))
	}
	rows, err := s.graph.Read(ctx, askHistoryCypher, map[string]any{"ids": unique(targets), "limit": k + 4})
	for _, r := range rows {
		if !s.allowed(ctx, r["acl"]) {
			continue
		}
		text := fmt.Sprintf("commit %s by %s on %s\n%s", str(r["sha"]), str(r["author"]), str(r["date"]), str(r["message"]))
		if t := list(r["tickets"]); t != "" {
			text += "\ntickets: " + t
		}
		g.add(evidence{ID: str(r["id"]), Label: "Commit", Title: str(r["title"]), Text: text})
	}
	return err
}

func (s *Server) askImpact(ctx context.Context, g *gatherer, symbols []string) error {
	if len(symbols) == 0 {
		return nil
	}
	id := symbols[0]
	rows, err := s.graph.Read(ctx, `MATCH (t:Code {id: $id})
		OPTIONAL MATCH p = (a:Symbol)-[:CALLS|USES_TYPE|IMPLEMENTS|EXTENDS|OVERRIDES*1..3]->(t) WHERE $repos IS NULL OR a.repo IN $repos
		WITH t, a, min(length(p)) AS d ORDER BY d, a.path
		WITH t, collect(CASE WHEN a IS NULL THEN NULL ELSE {name: a.qualname, path: a.path, d: d, test: a.path =~ $testPath,
		     route: a.route} END) AS affected
		RETURN coalesce(t.qualname, t.path) AS name, t.path AS path, affected`, map[string]any{"id": id, "testPath": testPathRegex})
	if err != nil || len(rows) == 0 {
		return err
	}
	r := rows[0]
	var code, tests, endpoints []string
	affected, _ := r["affected"].([]any)
	for _, a := range affected {
		m, _ := a.(map[string]any)
		line := fmt.Sprintf("%s (%s, %v hops)", str(m["name"]), str(m["path"]), m["d"])
		switch {
		case m["test"] == true:
			tests = append(tests, line)
		default:
			code = append(code, line)
			if str(m["route"]) != "" {
				endpoints = append(endpoints, str(m["route"]))
			}
		}
	}
	text := fmt.Sprintf("%d code symbols, %d tests and %d endpoints depend on %s within 3 hops.\ncode: %s\ntests: %s\nendpoints: %s",
		len(code), len(tests), len(endpoints), str(r["name"]), strings.Join(code[:min(len(code), 25)], "; "),
		strings.Join(tests[:min(len(tests), 10)], "; "), strings.Join(endpoints, ", "))
	g.add(evidence{ID: id, Label: "Impact", Title: "Impact of a change to " + str(r["name"]), Path: str(r["path"]), Text: text})
	return nil
}

func (s *Server) askTickets(ctx context.Context, g *gatherer, keys []string) error {
	for _, key := range keys[:min(len(keys), 3)] {
		rows, err := s.graph.Read(ctx, askTicketCypher, map[string]any{"id": "jira:" + strings.ToUpper(key)})
		if err != nil {
			return err
		}
		if len(rows) == 0 || !s.allowed(ctx, rows[0]["acl"]) {
			continue
		}
		t := rows[0]
		text := fmt.Sprintf("%s %s\n%s", str(t["type"]), str(t["status"]), str(t["text"]))
		if p := str(t["parent"]); p != "" {
			text += "\nparent: " + p
		}
		g.add(evidence{ID: str(t["id"]), Label: "Ticket", Title: str(t["key"]) + ": " + str(t["title"]), URL: str(t["url"]), Text: text})
		commits, _ := t["commits"].([]any)
		for _, c := range commits {
			m, _ := c.(map[string]any)
			if !s.allowed(ctx, m["acl"]) {
				continue
			}
			g.add(evidence{ID: str(m["id"]), Label: "Commit", Title: str(m["title"]),
				Text: fmt.Sprintf("commit by %s on %s references %s: %s", str(m["author"]), str(m["date"]), key, str(m["title"]))})
		}
	}
	return nil
}

func (s *Server) askRequirements(ctx context.Context, g *gatherer, refs []string, q, repo string) error {
	p := map[string]any{"refs": refs, "repo": repo}
	where := `(r.id IN $refs OR r.ref IN $refs)`
	if len(refs) == 0 {
		vec, err := s.emb.Query(ctx, q)
		if err != nil {
			return err
		}
		p["v"] = vec
		where = `r.id IN $matched`
		m, err := s.graph.Read(ctx, `CALL db.index.vector.queryNodes('requirement_embedding', 3, $v) YIELD node, score
			WHERE score >= 0.7 RETURN collect(node.id) AS ids`, p)
		if err != nil {
			return err
		}
		p["matched"] = one(m)["ids"]
	}
	rows, err := s.graph.Read(ctx, `MATCH (r:Requirement) WHERE `+where+` AND ($repo = '' OR r.repo = $repo) AND ($repos IS NULL OR r.repo IN $repos)
		OPTIONAL MATCH (r)-[e:IMPLEMENTED_BY|CONTRADICTED_BY]->(x:Symbol)
		WITH r, collect(CASE WHEN x IS NULL THEN NULL ELSE type(e) + ' ' + x.qualname + ' (' + x.path + ')' END)[..8] AS evidence
		RETURN r.id AS id, r.ref AS ref, r.statement AS statement, r.verdict AS verdict, r.reasoning AS reasoning,
		       r.gaps AS gaps, r.source AS source, evidence LIMIT 5`, p)
	for _, r := range rows {
		text := fmt.Sprintf("%s\nverdict: %s\nreasoning: %s", str(r["statement"]), str(r["verdict"]), str(r["reasoning"]))
		if gaps := list(r["gaps"]); gaps != "" {
			text += "\ngaps: " + gaps
		}
		if ev := list(r["evidence"]); ev != "" {
			text += "\nevidence: " + ev
		}
		g.add(evidence{ID: str(r["id"]), Label: "Requirement", Title: strings.TrimSpace(str(r["ref"]) + " " + cutString(str(r["statement"]), 80)),
			Path: str(r["source"]), Text: text})
	}
	return err
}

// askSummaries reads :Summary nodes (file, module, repo or domain level) when they exist.
func (s *Server) askSummaries(ctx context.Context, g *gatherer, q, repo string, k int) error {
	vec, err := s.emb.Query(ctx, q)
	if err != nil {
		return err
	}
	rows, err := s.graph.Read(ctx, askSummaryCypher, map[string]any{"v": vec, "k": k, "repo": repo, "chars": askItemChars})
	for _, r := range rows {
		if s.allowed(ctx, r["acl"]) {
			g.add(evidence{ID: str(r["id"]), Label: "Summary", Title: strings.TrimSpace(str(r["level"]) + " summary: " + str(r["title"])),
				Path: str(r["path"]), URL: str(r["url"]), Text: str(r["text"])})
		}
	}
	return err
}

// askConcepts is the part of domain_overview that matches the question: concepts and their rules.
func (s *Server) askConcepts(ctx context.Context, g *gatherer, q, repo string, k int) error {
	vec, err := s.emb.Query(ctx, q)
	if err != nil {
		return err
	}
	rows, err := s.graph.Read(ctx, `CALL db.index.vector.queryNodes('concept_embedding', $k, $v) YIELD node, score
		WHERE ($repo = '' OR node.repo = $repo) AND ($repos IS NULL OR node.repo IN $repos)
		OPTIONAL MATCH (node)<-[:REALISES]-(x:Symbol)
		WITH node, score, collect(DISTINCT x.qualname)[..6] AS code
		OPTIONAL MATCH (br:BusinessRule)-[:IMPLEMENTED_BY]->(:Symbol)-[:REALISES]->(node)
		RETURN node.id AS id, node.name AS name, node.kind AS kind, node.definition AS definition, code,
		       collect(DISTINCT br.statement)[..4] AS rules ORDER BY score DESC`, map[string]any{"v": vec, "k": k, "repo": repo})
	for _, r := range rows {
		text := strings.TrimSpace(str(r["kind"]) + ": " + str(r["definition"]))
		if c := list(r["code"]); c != "" {
			text += "\nrealised by: " + c
		}
		if c := list(r["rules"]); c != "" {
			text += "\nrules: " + c
		}
		g.add(evidence{ID: str(r["id"]), Label: "DomainConcept", Title: str(r["name"]), Text: text})
	}
	return err
}

// ---- answer -------------------------------------------------------------------------------------

const askSystem = "You answer questions about a software system for a developer. Use only the numbered context " +
	"items. Put the number of the supporting item in square brackets after each claim, for example [2] or [1][3]. " +
	"Use only numbers that appear in the context. When the context does not answer the question, say so in one " +
	"sentence and say what is missing. Write at most 250 words. Write code names and paths exactly as in the context."

var (
	citeRe   = regexp.MustCompile(`\[(\d+(?:\s*[,;]\s*\d+)*)\]`)
	numRe    = regexp.MustCompile(`\d+`)
	spaceRe  = regexp.MustCompile(` +([.,;:])`) // left after a removed citation
	unsureRe = regexp.MustCompile(`(?i)(does not (contain|answer|say|mention|include)|do not (contain|answer|say|mention|include)|not enough (information|context)|no (information|mention)|cannot (answer|determine|tell)|can't (answer|determine|tell)|is not (in|covered by) the context)`)
)

// buildPrompt numbers the items that fit in the context budget.
func buildPrompt(q string, items []evidence) (string, []evidence) {
	var b strings.Builder
	fmt.Fprintf(&b, "Question: %s\n\nContext:\n", q)
	used := 0
	var kept []evidence
	for _, e := range items {
		head := fmt.Sprintf("[%d] %s %s (id: %s", len(kept)+1, e.Label, e.Title, e.ID)
		if e.Path != "" {
			head += ", " + e.Path
		}
		if e.URL != "" {
			head += ", " + e.URL
		}
		block := head + ")\n" + cutString(e.Text, askItemChars) + "\n\n"
		if used+len(block) > askContextChars && len(kept) > 0 {
			break
		}
		used += len(block)
		kept = append(kept, e)
		b.WriteString(block)
	}
	return b.String(), kept
}

// checkCitations removes citation numbers outside 1..n from the text. It returns the cleaned text, the
// valid cited numbers and the removed numbers, both sorted.
func checkCitations(text string, n int) (string, []int, []int) {
	cited := map[int]bool{}
	invalid := map[int]bool{}
	text = citeRe.ReplaceAllStringFunc(text, func(m string) string {
		var ok []string
		for _, part := range numRe.FindAllString(m, -1) {
			c, _ := strconv.Atoi(part)
			if c >= 1 && c <= n {
				cited[c] = true
				ok = append(ok, part)
			} else {
				invalid[c] = true
			}
		}
		if len(ok) == 0 {
			return ""
		}
		return "[" + strings.Join(ok, ", ") + "]"
	})
	var valid, bad []int
	for c := range cited {
		valid = append(valid, c)
	}
	for c := range invalid {
		bad = append(bad, c)
	}
	sort.Ints(valid)
	sort.Ints(bad)
	return strings.TrimSpace(spaceRe.ReplaceAllString(text, "$1")), valid, bad
}

// answer calls the LLM and validates its citations against the numbered context.
func answer(ctx context.Context, llm *ChatLLM, q string, route Route, items []evidence) (AskOut, error) {
	out := AskOut{Route: route, Citations: []Citation{}, ToolsUsed: []string{}}
	if len(items) == 0 {
		out.Answer = "The knowledge graph has no context for this question. Try search_graph or search_knowledge with other words."
		out.Note = "no context retrieved; no LLM call"
		return out, nil
	}
	prompt, kept := buildPrompt(q, items)
	text, err := llm.Complete(ctx, askSystem, prompt, askMaxTokens)
	if err != nil {
		return out, err
	}
	var ns []int
	out.Answer, ns, out.InvalidCitations = checkCitations(text, len(kept))
	for _, n := range ns {
		e := kept[n-1]
		out.Citations = append(out.Citations, Citation{N: n, ID: e.ID, Label: e.Label, Title: e.Title, Path: e.Path,
			URL: e.URL, Excerpt: cutString(strings.TrimSpace(e.Text), askExcerptChars)})
	}
	if len(out.InvalidCitations) > 0 {
		out.Note = fmt.Sprintf("removed %d citation numbers that were not in the context", len(out.InvalidCitations))
	}
	out.Confidence = confidence(route, len(out.Citations), len(out.InvalidCitations), unsureRe.MatchString(out.Answer))
	return out, nil
}

// confidence is a heuristic in [0, 1]: cited evidence and a clear route raise it; invalid
// citations and an answer that says the context is not enough lower it.
func confidence(r Route, valid, invalid int, unsure bool) float64 {
	c := 0.1 + 0.6*float64(min(valid, 3))/3 + 0.3*r.Confidence
	if invalid > 0 {
		c -= 0.2
	}
	if valid == 0 {
		c = min(c, 0.2)
	}
	if unsure {
		c = min(c, 0.25)
	}
	return round2(max(0, min(1, c)))
}
