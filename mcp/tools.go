package main

import (
	"context"
	"fmt"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Server holds the shared clients of all tools.
type Server struct {
	graph       *Graph
	emb         *Embedder
	defaultRepo string
	groups      map[string]bool // ACL groups this server may see; see acl.go
}

func (s *Server) repo(r string) string {
	if r != "" {
		return r
	}
	return s.defaultRepo
}

func clamp(v, lo, hi, def int) int {
	if v == 0 {
		return def
	}
	return max(lo, min(hi, v))
}

func one(rows []map[string]any) map[string]any {
	if len(rows) == 0 {
		return nil
	}
	return rows[0]
}

// resolve accepts a node id, a qualified name or a short name and returns one symbol id.
func (s *Server) resolve(ctx context.Context, ref, repo string) (string, error) {
	if row := one(must(s.graph.Read(ctx, `MATCH (n:Code {id: $ref}) WHERE $repos IS NULL OR n.repo IN $repos RETURN n.id AS id`,
		map[string]any{"ref": ref}))); row != nil {
		return row["id"].(string), nil
	}
	rows, err := s.graph.Read(ctx, `MATCH (n:Symbol) WHERE (n.qualname = $ref OR n.name = $ref) AND ($repo = '' OR n.repo = $repo) AND ($repos IS NULL OR n.repo IN $repos)
		RETURN n.id AS id, n.kind AS kind, n.path AS path ORDER BY n.qualname = $ref DESC, n.path LIMIT 11`,
		map[string]any{"ref": ref, "repo": repo})
	if err != nil {
		return "", err
	}
	switch {
	case len(rows) == 0:
		return "", fmt.Errorf("no symbol %q; use find_symbol or search_graph to find it", ref)
	case len(rows) == 1:
		return rows[0]["id"].(string), nil
	}
	var b strings.Builder
	for _, r := range rows[:min(10, len(rows))] {
		fmt.Fprintf(&b, "\n- %v (%v)", r["id"], r["kind"])
	}
	return "", fmt.Errorf("%q is ambiguous; call again with one of these ids:%s", ref, b.String())
}

func must(rows []map[string]any, err error) []map[string]any {
	if err != nil {
		return nil
	}
	return rows
}

// ---- inputs and outputs -------------------------------------------------------------------------

type SearchIn struct {
	Query string `json:"query" jsonschema:"question or description in natural language, e.g. 'who can invite users in bulk'"`
	Repo  string `json:"repo,omitempty" jsonschema:"repository name; empty uses the default or searches all"`
	K     int    `json:"k,omitempty" jsonschema:"number of results, 1-20 (default 8)"`
}
type FindSymbolIn struct {
	Name  string `json:"name" jsonschema:"symbol name, qualified name (Class.method) or prefix; * wildcards allowed"`
	Repo  string `json:"repo,omitempty" jsonschema:"repository name"`
	Kind  string `json:"kind,omitempty" jsonschema:"Class, Interface, Enum, Function or Method"`
	Limit int    `json:"limit,omitempty" jsonschema:"maximum results, 1-50 (default 20)"`
}
type SymbolIn struct {
	Symbol string `json:"symbol" jsonschema:"symbol id (from another tool) or qualified name"`
	Repo   string `json:"repo,omitempty" jsonschema:"repository name, to disambiguate names"`
}
type ImpactIn struct {
	Symbol string `json:"symbol" jsonschema:"symbol id or qualified name of the code you plan to change"`
	Repo   string `json:"repo,omitempty" jsonschema:"repository name"`
	Depth  int    `json:"depth,omitempty" jsonschema:"how many hops of callers and subtypes to follow, 1-4 (default 3)"`
}
type NeighborsIn struct {
	NodeID    string   `json:"node_id" jsonschema:"id of any node (symbol, file, concept, rule, requirement)"`
	EdgeTypes []string `json:"edge_types,omitempty" jsonschema:"filter, e.g. CALLS, IMPORTS, DEFINES, CONTAINS, REALISES, IMPLEMENTED_BY"`
	Direction string   `json:"direction,omitempty" jsonschema:"out, in or both (default both)"`
	Depth     int      `json:"depth,omitempty" jsonschema:"1-3 (default 1)"`
	Limit     int      `json:"limit,omitempty" jsonschema:"1-100 (default 50)"`
}
type RequirementIn struct {
	Requirement string `json:"requirement" jsonschema:"requirement id, document reference (e.g. FR-P10-03-02) or the requirement text"`
	Repo        string `json:"repo,omitempty" jsonschema:"repository name"`
}
type RepoIn struct {
	Repo string `json:"repo,omitempty" jsonschema:"repository name; empty lists repositories"`
}
type CypherIn struct {
	Query  string         `json:"query" jsonschema:"read-only Cypher; nodes have :Code (File, Symbol, Chunk, Module), :Domain (DomainConcept, BusinessRule, Capability) or :Requirement labels"`
	Params map[string]any `json:"params,omitempty" jsonschema:"query parameters"`
}

type Rows struct {
	Results []map[string]any `json:"results"`
	Note    string           `json:"note,omitempty"`
}
type Doc struct {
	Data map[string]any `json:"data"`
}

// ---- tools --------------------------------------------------------------------------------------

func (s *Server) register(m *mcp.Server) {
	s.registerSources(m)
	s.registerContracts(m)
	mcp.AddTool(m, &mcp.Tool{Name: "search_graph", Description: "Find code for a question in natural language. " +
		"Hybrid search (vectors + full text + domain concepts) over code chunks, then graph expansion: each result has the " +
		"symbol, its domain concepts, business rules, callers, callees, related tests and a code excerpt. Start here."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in SearchIn) (*mcp.CallToolResult, Rows, error) {
			hits, err := s.Candidates(ctx, s.repo(in.Repo), in.Query, clamp(in.K, 1, 20, 8))
			if err != nil {
				return nil, Rows{}, err
			}
			chunkIDs := make([]string, len(hits))
			for i, h := range hits {
				chunkIDs[i] = h.ID
			}
			rows, err := s.Expand(ctx, chunkIDs, 1500)
			if err != nil {
				return nil, Rows{}, err
			}
			for i := range rows {
				rows[i]["score"] = hits[i].Score
			}
			return nil, Rows{Results: rows}, nil
		})

	mcp.AddTool(m, &mcp.Tool{Name: "find_symbol", Description: "Look up classes, interfaces, functions and methods by name. " +
		"Exact name and qualified-name matches come first, then full-text matches."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in FindSymbolIn) (*mcp.CallToolResult, Rows, error) {
			q := lucene(in.Name)
			if w := wildcardRe.FindString(in.Name); w != "" {
				q += " OR " + w // keep a prefix* query such as Invit* unescaped
			}
			p := map[string]any{"name": in.Name, "repo": s.repo(in.Repo), "kind": in.Kind, "limit": clamp(in.Limit, 1, 50, 20), "q": q}
			rows, err := s.graph.Read(ctx, `
				CALL () {
				  MATCH (n:Symbol) WHERE (n.name = $name OR n.qualname = $name) RETURN n, 2.0 AS score
				  UNION
				  CALL db.index.fulltext.queryNodes('symbol_text', $q, {limit: 100}) YIELD node, score RETURN node AS n, score / 100.0 AS score
				}
				WITH n, max(score) AS score WHERE ($repo = '' OR n.repo = $repo) AND ($repos IS NULL OR n.repo IN $repos) AND ($kind = '' OR n.kind = $kind)
				RETURN n.id AS id, n.qualname AS qualname, n.kind AS kind, n.role AS role, n.route AS route,
				       n.path AS path, n.start_line AS start_line, n.signature AS signature, n.repo AS repo
				ORDER BY score DESC, n.path LIMIT $limit`, p)
			return nil, Rows{Results: rows}, err
		})

	mcp.AddTool(m, &mcp.Tool{Name: "get_symbol_code", Description: "Full source code of one symbol (all of its chunks), " +
		"with file path, line range, signature and annotations."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in SymbolIn) (*mcp.CallToolResult, Doc, error) {
			id, err := s.resolve(ctx, in.Symbol, s.repo(in.Repo))
			if err != nil {
				return nil, Doc{}, err
			}
			row := one(must(s.graph.Read(ctx, `MATCH (n:Code {id: $id})
				OPTIONAL MATCH (c:Chunk)-[:PART_OF]->(n) WITH n, c ORDER BY c.start_line
				RETURN n.id AS id, n.qualname AS qualname, n.kind AS kind, n.path AS path, n.start_line AS start_line,
				       n.end_line AS end_line, n.signature AS signature, n.annotations AS annotations, n.role AS role,
				       left(reduce(code = '', t IN collect(c.text) | code + t + '\n'), 30000) AS code`,
				map[string]any{"id": id})))
			if row == nil {
				return nil, Doc{}, fmt.Errorf("symbol %s not found", id)
			}
			return nil, Doc{Data: row}, nil
		})

	mcp.AddTool(m, &mcp.Tool{Name: "impact_analysis", Description: "What can break if a symbol changes: every caller, " +
		"subtype and user up to N hops (also HTTP callers in other repositories, HTTP tests and consumers of topics it " +
		"publishes to), split into production code and tests, plus affected HTTP endpoints, " +
		"linked requirements and domain concepts. Use before you edit shared code."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in ImpactIn) (*mcp.CallToolResult, Doc, error) {
			id, err := s.resolve(ctx, in.Symbol, s.repo(in.Repo))
			if err != nil {
				return nil, Doc{}, err
			}
			depth := clamp(in.Depth, 1, 4, 3)
			affected, err := s.graph.Read(ctx, fmt.Sprintf(`
				MATCH (t:Code {id: $id})
				MATCH p = (a:Symbol|File)-[:%s*1..%d]->(t)
				WHERE $repos IS NULL OR a.repo IN $repos
				WITH a, min(length(p)) AS distance
				RETURN a.id AS id, coalesce(a.qualname, a.path) AS qualname, a.kind AS kind, a.path AS path, a.repo AS repo,
				       CASE WHEN a.endpoint_route IS NOT NULL THEN 'endpoint' ELSE a.role END AS role,
				       coalesce(a.http_method, a.endpoint_method) AS http_method, coalesce(a.route, a.endpoint_route) AS route,
				       distance, a.path =~ $testPath AS is_test
				ORDER BY distance, a.path LIMIT 300`, impactEdges, depth), map[string]any{"id": id, "testPath": testPathRegex})
			if err != nil {
				return nil, Doc{}, err
			}
			// Consumers of topics that the symbol publishes to are affected across the broker.
			for _, sub := range s.topicSubscribers(ctx, []string{id}) {
				sub["distance"], sub["via"] = 1, "topic:"+fmt.Sprint(sub["topic"])
				affected = append(affected, sub)
			}
			code, tests, endpoints := []map[string]any{}, []map[string]any{}, []map[string]any{}
			ids := []string{id}
			for _, a := range affected {
				ids = append(ids, a["id"].(string))
				if a["is_test"] == true {
					tests = append(tests, a)
					continue
				}
				code = append(code, a)
				if a["role"] == "endpoint" {
					endpoints = append(endpoints, a)
				}
			}
			reqs := must(s.graph.Read(ctx, `MATCH (r:Requirement)-[e:IMPLEMENTED_BY|CONTRADICTED_BY]->(x) WHERE x.id IN $ids
				RETURN DISTINCT r.id AS id, r.ref AS ref, r.statement AS statement, r.verdict AS verdict, type(e) AS link`,
				map[string]any{"ids": ids}))
			concepts := must(s.graph.Read(ctx, `MATCH (x)-[:REALISES]->(c:DomainConcept) WHERE x.id IN $ids
				RETURN c.name AS concept, c.kind AS kind, count(DISTINCT x) AS symbols ORDER BY symbols DESC LIMIT 20`,
				map[string]any{"ids": ids}))
			return nil, Doc{Data: map[string]any{
				"symbol": id, "depth": depth, "affected_code": code, "affected_tests": tests,
				"affected_endpoints": endpoints, "requirements": reqs, "concepts": concepts,
				"summary": fmt.Sprintf("%d code symbols, %d tests, %d endpoints, %d requirements within %d hops",
					len(code), len(tests), len(endpoints), len(reqs), depth),
			}}, nil
		})

	mcp.AddTool(m, &mcp.Tool{Name: "find_related_tests", Description: "Tests that exercise a symbol: test code that " +
		"calls or uses it directly or through up to 2 intermediate calls, including HTTP tests of the endpoints that " +
		"reach it (TestClient, supertest, MockMvc, httptest, ...). Run these after you change the symbol."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in SymbolIn) (*mcp.CallToolResult, Rows, error) {
			id, err := s.resolve(ctx, in.Symbol, s.repo(in.Repo))
			if err != nil {
				return nil, Rows{}, err
			}
			rows, err := s.graph.Read(ctx, `
				MATCH (t:Code {id: $id})
				CALL (t) {
				  MATCH p = (x:Symbol|File)-[:`+testEdges+`*1..3]->(t) RETURN x, length(p) AS distance, 'code' AS via
				  UNION
				  // tests that drive a publisher of a topic this symbol consumes
				  MATCH (t)-[:SUBSCRIBES]->(:Topic)<-[:PUBLISHES]-(pub)
				  MATCH p = (x:Symbol|File)-[:`+testEdges+`*1..2]->(pub) RETURN x, length(p) + 1 AS distance, 'topic' AS via
				}
				WITH x, distance, via WHERE x.path =~ $testPath AND ($repos IS NULL OR x.repo IN $repos)
				WITH x, min(distance) AS distance, collect(DISTINCT via) AS via
				RETURN x.id AS id, coalesce(x.qualname, x.path) AS test, x.path AS path, x.repo AS repo, x.start_line AS line,
				       distance, via
				ORDER BY distance, x.path LIMIT 50`, map[string]any{"id": id, "testPath": testPathRegex})
			note := ""
			if err == nil && len(rows) == 0 {
				note = "no tests found in the call graph; the symbol may be tested only end to end"
			}
			return nil, Rows{Results: rows, Note: note}, err
		})

	mcp.AddTool(m, &mcp.Tool{Name: "get_neighbors", Description: "Walk the graph from any node: typed edges " +
		"(CALLS, IMPORTS, DEFINES, CONTAINS, EXTENDS, IMPLEMENTS, USES_TYPE, REALISES, IMPLEMENTED_BY, VALIDATED_BY, COVERS, " +
		contractDoc + ", ...)."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in NeighborsIn) (*mcp.CallToolResult, Rows, error) {
			if err := checkEdgeTypes(in.EdgeTypes); err != nil {
				return nil, Rows{}, err
			}
			rel := ""
			if len(in.EdgeTypes) > 0 {
				rel = ":" + strings.Join(in.EdgeTypes, "|")
			}
			hop := fmt.Sprintf("[r%s*1..%d]", rel, clamp(in.Depth, 1, 3, 1))
			pattern := map[string]string{"out": "-" + hop + "->", "in": "<-" + hop + "-"}[in.Direction]
			if pattern == "" {
				pattern = "-" + hop + "-"
			}
			rows, err := s.graph.Read(ctx, `
				CALL () { MATCH (n:Code {id: $id}) RETURN n UNION MATCH (n:Domain {id: $id}) RETURN n
				          UNION MATCH (n:Requirement {id: $id}) RETURN n UNION MATCH (n:Source {id: $id}) RETURN n }
				WITH n WHERE $repos IS NULL OR n.repo IS NULL OR n.repo IN $repos
				MATCH p = (n)`+pattern+`(m)
				WHERE $repos IS NULL OR m.repo IS NULL OR m.repo IN $repos
				WITH m, min(length(p)) AS distance, head(collect([x IN relationships(p) | type(x)])) AS via
				RETURN m.id AS id, labels(m) AS labels, coalesce(m.qualname, m.name, m.statement, m.title, m.path) AS name,
				       m.path AS path, distance, via, m.acl AS acl
				ORDER BY distance, name LIMIT $limit`, map[string]any{"id": in.NodeID, "limit": clamp(in.Limit, 1, 100, 50)})
			out := []map[string]any{}
			for _, r := range rows {
				if s.allowed(r["acl"]) {
					delete(r, "acl")
					out = append(out, r)
				}
			}
			return nil, Rows{Results: out}, err
		})

	mcp.AddTool(m, &mcp.Tool{Name: "trace_requirement", Description: "Trace a document requirement to code: its " +
		"verdict (supported, contradicted, unresolved), reasoning, gaps, the implementing or contradicting symbols and " +
		"the tests that validate it. Accepts an id, a reference such as FR-P10-03-02, or the requirement text."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in RequirementIn) (*mcp.CallToolResult, Doc, error) {
			p := map[string]any{"x": in.Requirement, "repo": s.repo(in.Repo)}
			req := one(must(s.graph.Read(ctx, `MATCH (r:Requirement) WHERE (r.id = $x OR r.ref = $x) AND ($repo = '' OR r.repo = $repo) AND ($repos IS NULL OR r.repo IN $repos)
				RETURN r.id AS id LIMIT 1`, p)))
			if req == nil {
				vec, err := s.emb.Query(ctx, in.Requirement)
				if err != nil {
					return nil, Doc{}, err
				}
				p["v"] = vec
				matches := must(s.graph.Read(ctx, `CALL db.index.vector.queryNodes('requirement_embedding', 5, $v) YIELD node, score
					WHERE ($repo = '' OR node.repo = $repo) AND ($repos IS NULL OR node.repo IN $repos)
					RETURN node.id AS id, node.ref AS ref, node.statement AS statement, score ORDER BY score DESC`, p))
				if len(matches) == 0 {
					return nil, Doc{}, fmt.Errorf("no checked requirements match; run kg-verify on the document first")
				}
				if matches[0]["score"].(float64) < 0.85 {
					return nil, Doc{Data: map[string]any{"note": "no exact match; closest checked requirements",
						"candidates": matches}}, nil
				}
				req = matches[0]
			}
			p["id"] = req["id"]
			data := one(must(s.graph.Read(ctx, `MATCH (r:Requirement {id: $id})
				OPTIONAL MATCH (r)-[e:IMPLEMENTED_BY|CONTRADICTED_BY]->(s:Symbol)
				WITH r, collect(CASE WHEN s IS NULL THEN NULL ELSE {link: type(e), symbol_id: s.id, symbol: s.qualname,
				     path: s.path, explanation: e.explanation, confidence: e.confidence} END) AS evidence
				OPTIONAL MATCH (r)-[:VALIDATED_BY]->(t:Symbol)
				RETURN r.id AS id, r.ref AS ref, r.statement AS statement, r.type AS type, r.source AS source,
				       r.section AS section, r.verdict AS verdict, r.confidence AS confidence, r.reasoning AS reasoning,
				       r.gaps AS gaps, r.checked_at AS checked_at, evidence,
				       collect(DISTINCT {test: t.qualname, path: t.path}) AS tests`, p)))
			return nil, Doc{Data: data}, nil
		})

	mcp.AddTool(m, &mcp.Tool{Name: "domain_overview", Description: "Business view of a repository: capabilities per " +
		"module, main domain concepts, business rules, entities and HTTP endpoints. Without repo: list repositories."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in RepoIn) (*mcp.CallToolResult, Doc, error) {
			repo := s.repo(in.Repo)
			if repo != "" && !s.allowedRepo(repo) {
				return nil, Doc{}, fmt.Errorf("repository %s is not visible to this server", repo)
			}
			if repo == "" {
				rows, err := s.graph.Read(ctx, `MATCH (r:Repository) WHERE $repos IS NULL OR r.name IN $repos
					OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
					RETURN r.name AS repo, r.commit AS commit, r.scip_tools AS scip_tools, count(f) AS files ORDER BY repo`, nil)
				return nil, Doc{Data: map[string]any{"repositories": rows}}, err
			}
			p := map[string]any{"repo": repo}
			return nil, Doc{Data: map[string]any{
				"repo": repo,
				"capabilities": must(s.graph.Read(ctx, `MATCH (c:Capability {repo: $repo})
					RETURN c.module AS module, c.name AS name, c.summary AS summary ORDER BY module`, p)),
				"concepts": must(s.graph.Read(ctx, `MATCH (c:DomainConcept {repo: $repo}) OPTIONAL MATCH (c)<-[:REALISES]-(s)
					RETURN c.name AS name, c.kind AS kind, c.definition AS definition, count(s) AS symbols
					ORDER BY symbols DESC LIMIT 40`, p)),
				"rules": must(s.graph.Read(ctx, `MATCH (r:BusinessRule {repo: $repo})
					RETURN r.module AS module, r.statement AS statement ORDER BY module LIMIT 60`, p)),
				"entities": must(s.graph.Read(ctx, `MATCH (s:Symbol {repo: $repo, role: 'entity'})
					RETURN s.qualname AS entity, s.path AS path ORDER BY path`, p)),
				"endpoints": must(s.graph.Read(ctx, `MATCH (s:Symbol {repo: $repo, role: 'endpoint'})
					RETURN s.http_method AS method, s.route AS route, s.qualname AS handler, s.path AS path
					ORDER BY path, route LIMIT 200`, p)),
			}}, nil
		})

	mcp.AddTool(m, &mcp.Tool{Name: "read_cypher", Description: "Run a read-only Cypher query when the other tools do " +
		"not fit. Writes and most procedures are rejected; at most 500 rows; 15 s timeout. Labels: File, Symbol " +
		"(Class, Interface, Enum, Function, Method), Chunk, Module, Repository, DomainConcept, BusinessRule, Capability, " +
		"Requirement, ApiOperation, Schema, Table, Column, Topic. Edges: CONTAINS, DEFINES, PART_OF, IMPORTS, CALLS, " +
		"USES_TYPE, EXTENDS, IMPLEMENTS, OVERRIDES, REALISES, IMPLEMENTED_BY, CONTRADICTED_BY, VALIDATED_BY, COVERS, " +
		"USES_CONCEPT, " + contractDoc + "."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in CypherIn) (*mcp.CallToolResult, Rows, error) {
			if !s.groups["*"] {
				return nil, Rows{}, fmt.Errorf("read_cypher is off for scoped servers (KG_ACL_GROUPS is not *)")
			}
			if err := checkReadOnly(in.Query); err != nil {
				return nil, Rows{}, err
			}
			rows, err := s.graph.Read(ctx, in.Query, in.Params)
			return nil, Rows{Results: rows}, err
		})
}
