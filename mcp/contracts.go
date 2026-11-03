package main

import (
	"context"
	"fmt"
	"regexp"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Edge types that impact_analysis and find_related_tests follow from a dependant to what it depends on.
// CALLS_HTTP (client -> endpoint or ApiOperation) and TESTS (HTTP test -> endpoint) come from the
// pipeline's links step; IMPLEMENTS also joins an endpoint to its ApiOperation. Keep these lists in
// sync with edgeTypesDoc and with the pipeline (kg_pipeline/code/links.py).
const (
	impactEdges = "CALLS|USES_TYPE|IMPLEMENTS|EXTENDS|OVERRIDES|CALLS_HTTP|TESTS"
	testEdges   = "CALLS|USES_TYPE|CALLS_HTTP|TESTS"
	contractDoc = "CALLS_HTTP, TESTS, PUBLISHES, SUBSCRIBES, DESCRIBES, USES_SCHEMA, HAS_COLUMN, FOREIGN_KEY, MAPS_TO, READS, WRITES, MIGRATES"
)

type ContractIn struct {
	Contract string `json:"contract" jsonschema:"ApiOperation or Table or Topic id, operationId, 'POST /api/orders', a table name or a topic name; an endpoint symbol id also works"`
	Repo     string `json:"repo,omitempty" jsonschema:"repository name, to disambiguate"`
}

var (
	routeLine  = regexp.MustCompile(`^(?i)(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)$`)
	routeParam = regexp.MustCompile(`\$\{[^}]*\}|\{[^}]*\}|<[^>]*>|/:[A-Za-z_]\w*|\[[^\]]+\]`)
)

// normRoute mirrors kg_pipeline.code.httpcalls.norm_route: parameters become {}, lower case, no trailing slash.
func normRoute(p string) string {
	p, _, _ = strings.Cut(p, "?")
	p = routeParam.ReplaceAllStringFunc(p, func(m string) string {
		if strings.HasPrefix(m, "/:") {
			return "/{}"
		}
		return "{}"
	})
	parts := []string{}
	for _, s := range strings.Split(strings.ToLower(p), "/") {
		if s != "" {
			parts = append(parts, s)
		}
	}
	return "/" + strings.Join(parts, "/")
}

// findContract returns the id and kind (operation, endpoint, table, topic) of a contract reference.
func (s *Server) findContract(ctx context.Context, ref, repo string) (string, string, error) {
	p := map[string]any{"ref": ref, "repo": repo, "lower": strings.ToLower(ref)}
	rows, err := s.graph.Read(ctx, `
		MATCH (n:Code) WHERE (n.id = $ref
		   OR (n:ApiOperation AND (n.operation_id = $ref OR n.name = $ref))
		   OR (n:Table AND n.name = $lower) OR (n:Topic AND n.name = $ref))
		  AND ($repo = '' OR n.repo = $repo OR n:Topic) AND ($repos IS NULL OR n.repo IN $repos OR n:Topic)
		RETURN n.id AS id, CASE WHEN n:ApiOperation THEN 'operation' WHEN n:Table THEN 'table'
		       WHEN n:Topic THEN 'topic' WHEN n.endpoint_route IS NOT NULL THEN 'endpoint' ELSE 'symbol' END AS kind
		ORDER BY n.id = $ref DESC LIMIT 11`, p)
	if err != nil {
		return "", "", err
	}
	if m := routeLine.FindStringSubmatch(strings.TrimSpace(ref)); len(rows) == 0 && m != nil {
		cands, err := s.graph.Read(ctx, `
			MATCH (n:Code) WHERE ((n:ApiOperation AND n.method = $m) OR n.endpoint_method IN [$m, 'ANY'])
			  AND ($repo = '' OR n.repo = $repo) AND ($repos IS NULL OR n.repo IN $repos)
			RETURN n.id AS id, coalesce(n.path_template, n.endpoint_route) AS route,
			       CASE WHEN n:ApiOperation THEN 'operation' ELSE 'endpoint' END AS kind
			ORDER BY kind DESC, n.id LIMIT 500`, map[string]any{"m": strings.ToUpper(m[1]), "repo": repo})
		if err != nil {
			return "", "", err
		}
		want := normRoute(m[2])
		for _, c := range cands {
			if r, _ := c["route"].(string); normRoute(r) == want {
				rows = append(rows, c)
			}
		}
	}
	switch {
	case len(rows) == 0:
		return "", "", fmt.Errorf("no contract %q; give an operationId, 'METHOD /path', a table or a topic name", ref)
	case len(rows) == 1 || rows[0]["id"] == ref:
		return rows[0]["id"].(string), rows[0]["kind"].(string), nil
	}
	var b strings.Builder
	for _, r := range rows[:min(10, len(rows))] {
		fmt.Fprintf(&b, "\n- %v (%v)", r["id"], r["kind"])
	}
	return "", "", fmt.Errorf("%q is ambiguous; call again with one of these ids:%s", ref, b.String())
}

func (s *Server) registerContracts(m *mcp.Server) {
	mcp.AddTool(m, &mcp.Tool{Name: "get_contract", Description: "API contract or database table with everything " +
		"around it: for an HTTP operation (OpenAPI or code endpoint) its schemas, implementing endpoints, callers in any " +
		"repository and HTTP tests; for a table its columns, foreign keys, mapped ORM classes, readers, writers and " +
		"migrations; for a message topic its publishers, subscribers and AsyncAPI specs. Use it before you change an " +
		"API, a schema or a table that other services may use."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in ContractIn) (*mcp.CallToolResult, Doc, error) {
			id, kind, err := s.findContract(ctx, in.Contract, s.repo(in.Repo))
			if err != nil {
				return nil, Doc{}, err
			}
			p := map[string]any{"id": id, "testPath": testPathRegex}
			data := map[string]any{"id": id, "kind": kind}
			data["node"] = one(must(s.graph.Read(ctx, `MATCH (n:Code {id: $id}) RETURN n`, p)))["n"]
			// Rows of code that point at the contract (or at its implementers) by one edge type.
			edgeRows := func(query string) []map[string]any {
				return must(s.graph.Read(ctx, query+`
					WHERE $repos IS NULL OR x.repo IN $repos
					RETURN DISTINCT x.id AS id, coalesce(x.qualname, x.path) AS name, x.path AS path, x.repo AS repo,
					       x.start_line AS line, e.confidence AS confidence, e.method AS method, e.route AS route,
					       e.cross_repo AS cross_repo, e.via AS via ORDER BY repo, path LIMIT 100`, p))
			}
			switch kind {
			case "operation", "endpoint", "symbol":
				data["schemas"] = must(s.graph.Read(ctx, `
					MATCH (n:Code {id: $id}) OPTIONAL MATCH (n)-[:IMPLEMENTS]->(op:ApiOperation)
					WITH n, collect(op) AS ops UNWIND [n] + ops AS o
					MATCH (o)-[u:USES_SCHEMA]->(sc:Schema)
					RETURN DISTINCT sc.id AS id, sc.name AS name, u.role AS role, sc.properties AS properties,
					       sc.required AS required, left(sc.text, 1500) AS definition`, p))
				data["operations"] = edgeRows(`MATCH (n:Code {id: $id})-[e:IMPLEMENTS]->(x:ApiOperation)`)
				data["implementers"] = edgeRows(`MATCH (x:Code)-[e:IMPLEMENTS]->(n:ApiOperation {id: $id})`)
				// Targets: the contract plus its implementing endpoints, so callers of either are listed.
				targets := `MATCH (n:Code {id: $id}) OPTIONAL MATCH (impl)-[:IMPLEMENTS]->(n:ApiOperation)
					WITH collect(DISTINCT n) + collect(DISTINCT impl) AS ts UNWIND ts AS t `
				data["callers"] = edgeRows(targets + `MATCH (x:Code)-[e:CALLS_HTTP]->(t)`)
				data["tests"] = edgeRows(targets + `MATCH (x:Code)-[e:TESTS]->(t)`)
				data["summary"] = fmt.Sprintf("%d implementers, %d callers, %d HTTP tests",
					len(data["implementers"].([]map[string]any)), len(data["callers"].([]map[string]any)), len(data["tests"].([]map[string]any)))
			case "table":
				data["columns"] = must(s.graph.Read(ctx, `MATCH (:Table {id: $id})-[:HAS_COLUMN]->(c:Column)
					RETURN c.name AS name, c.data_type AS type, c.nullable AS nullable, c.primary_key AS primary_key ORDER BY c.name`, p))
				data["references"] = must(s.graph.Read(ctx, `MATCH (:Table {id: $id})-[e:FOREIGN_KEY]->(t:Table)
					RETURN t.id AS id, t.name AS table, e.column AS column`, p))
				data["referenced_by"] = must(s.graph.Read(ctx, `MATCH (t:Table)-[e:FOREIGN_KEY]->(:Table {id: $id})
					RETURN t.id AS id, t.name AS table, e.column AS column`, p))
				data["mapped_by"] = edgeRows(`MATCH (x:Code)-[e:MAPS_TO]->(n:Table {id: $id})`)
				data["readers"] = edgeRows(`MATCH (x:Code)-[e:READS]->(n:Table {id: $id})`)
				data["writers"] = edgeRows(`MATCH (x:Code)-[e:WRITES]->(n:Table {id: $id})`)
				data["migrations"] = edgeRows(`MATCH (x:File)-[e:DEFINES|MIGRATES]->(n:Table {id: $id})`)
				data["tests"] = must(s.graph.Read(ctx, fmt.Sprintf(`
					MATCH (:Table {id: $id})<-[:READS|WRITES|MAPS_TO]-(u)
					MATCH q = (x:Symbol|File)-[:%s*1..3]->(u) WHERE x.path =~ $testPath AND ($repos IS NULL OR x.repo IN $repos)
					WITH x, min(length(q)) AS distance
					RETURN x.id AS id, coalesce(x.qualname, x.path) AS test, x.path AS path, distance ORDER BY distance, path LIMIT 50`,
					testEdges), p))
			case "topic":
				data["publishers"] = edgeRows(`MATCH (x:Code)-[e:PUBLISHES]->(n:Topic {id: $id})`)
				data["subscribers"] = edgeRows(`MATCH (x:Code)-[e:SUBSCRIBES]->(n:Topic {id: $id})`)
				data["specs"] = edgeRows(`MATCH (x:ApiOperation)-[e:DESCRIBES]->(n:Topic {id: $id})`)
			}
			return nil, Doc{Data: data}, nil
		})
}

// topicSubscribers: symbols that consume a topic that one of ids publishes to (impact across a broker).
func (s *Server) topicSubscribers(ctx context.Context, ids []string) []map[string]any {
	return must(s.graph.Read(ctx, `
		MATCH (p:Code)-[:PUBLISHES]->(t:Topic)<-[:SUBSCRIBES]-(a:Code)
		WHERE p.id IN $ids AND NOT a.id IN $ids AND ($repos IS NULL OR a.repo IN $repos)
		RETURN DISTINCT a.id AS id, a.qualname AS qualname, a.kind AS kind, a.path AS path, a.repo AS repo,
		       t.name AS topic, a.path =~ $testPath AS is_test
		ORDER BY a.repo, a.path LIMIT 100`, map[string]any{"ids": ids, "testPath": testPathRegex}))
}
