package main

import (
	"context"
	"regexp"
	"sort"
	"strings"
)

// Same retrieval as the Python pipeline (kg_pipeline/search.py): three rankings fused with RRF,
// then expanded through the graph.
const rrfK = 60

// Test code: tests/ folders, *_test.go, *.spec.ts, test_*.py, FooTest.java, ...
const testPathRegex = `(?i).*(^|/)(tests?|__tests__|spec)/.*|.*(_test|_spec|\.test|\.spec|Test|Tests)\.[a-z]+$|.*(^|/)test_[^/]*$`

var (
	wordRe     = regexp.MustCompile(`[A-Za-z0-9_]{3,}`)
	luceneRe   = regexp.MustCompile(`([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)`)
	wildcardRe = regexp.MustCompile(`[A-Za-z0-9_]{2,}\*`)
)

func lucene(text string) string {
	words := wordRe.FindAllString(text, 30)
	for i, w := range words {
		words[i] = luceneRe.ReplaceAllString(w, `\$1`)
	}
	if len(words) == 0 {
		return "*"
	}
	return strings.Join(words, " OR ")
}

type scored struct {
	ID    string
	Score float64
}

func rrf(rankings ...[]string) []scored {
	scores := map[string]float64{}
	for _, ids := range rankings {
		for rank, id := range ids {
			scores[id] += 1.0 / float64(rrfK+rank+1)
		}
	}
	out := make([]scored, 0, len(scores))
	for id, s := range scores {
		out = append(out, scored{id, s})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Score != out[j].Score {
			return out[i].Score > out[j].Score
		}
		return out[i].ID < out[j].ID
	})
	return out
}

func ids(rows []map[string]any) []string {
	out := make([]string, 0, len(rows))
	for _, r := range rows {
		if s, ok := r["id"].(string); ok {
			out = append(out, s)
		}
	}
	return out
}

// Candidates returns the top chunk ids for a question.
func (s *Server) Candidates(ctx context.Context, repo, query string, k int) ([]scored, error) {
	vec, err := s.emb.Query(ctx, query)
	if err != nil {
		return nil, err
	}
	p := map[string]any{"v": vec, "repo": repo, "q": lucene(query)}
	byVector, err := s.graph.Read(ctx, `CALL db.index.vector.queryNodes('chunk_embedding', 80, $v) YIELD node, score
		WHERE ($repo = '' OR node.repo = $repo) AND ($repos IS NULL OR node.repo IN $repos) RETURN node.id AS id ORDER BY score DESC LIMIT 40`, p)
	if err != nil {
		return nil, err
	}
	byText, err := s.graph.Read(ctx, `CALL db.index.fulltext.queryNodes('chunk_text', $q, {limit: 80}) YIELD node, score
		WHERE ($repo = '' OR node.repo = $repo) AND ($repos IS NULL OR node.repo IN $repos) RETURN node.id AS id ORDER BY score DESC LIMIT 40`, p)
	if err != nil {
		return nil, err
	}
	byConcept, err := s.graph.Read(ctx, `CALL db.index.vector.queryNodes('concept_embedding', 10, $v) YIELD node, score
		WHERE ($repo = '' OR node.repo = $repo) AND ($repos IS NULL OR node.repo IN $repos)
		MATCH (node)<-[:REALISES]-(:Symbol)<-[:PART_OF]-(c:Chunk)
		RETURN c.id AS id, max(score) AS s ORDER BY s DESC LIMIT 40`, p)
	if err != nil {
		return nil, err
	}
	fused := rrf(ids(byVector), ids(byText), ids(byConcept))
	if len(fused) > k {
		fused = fused[:k]
	}
	return fused, nil
}

// Expand builds the evidence pack for chunk ids, in the same order.
func (s *Server) Expand(ctx context.Context, chunkIDs []string, codeChars int) ([]map[string]any, error) {
	rows, err := s.graph.Read(ctx, `
		UNWIND range(0, size($ids) - 1) AS i
		MATCH (c:Chunk {id: $ids[i]})-[:PART_OF]->(o)
		OPTIONAL MATCH (o)-[:REALISES]->(dc:DomainConcept)
		OPTIONAL MATCH (br:BusinessRule)-[:IMPLEMENTED_BY]->(o)
		WITH i, c, o, collect(DISTINCT dc.name) AS concepts, collect(DISTINCT br.statement) AS rules
		OPTIONAL MATCH (o)<-[:CALLS]-(caller:Symbol)
		WITH i, c, o, concepts, rules, collect(DISTINCT caller.qualname)[..6] AS callers
		OPTIONAL MATCH (o)-[:CALLS]->(callee:Symbol)
		WITH i, c, o, concepts, rules, callers, collect(DISTINCT callee.qualname)[..6] AS callees
		OPTIONAL MATCH (t:Symbol)-[:CALLS*1..2]->(o) WHERE t.path =~ $testPath
		WITH i, c, o, concepts, rules, callers, callees, collect(DISTINCT t.qualname)[..3] AS tests
		RETURN c.path AS path, c.start_line AS start_line, c.end_line AS end_line,
		       o.id AS symbol_id, coalesce(o.qualname, o.path) AS symbol, coalesce(o.kind, 'File') AS kind, o.role AS role, o.route AS route,
		       concepts, rules, callers, callees, tests, left(c.text, $chars) AS code,
		       o.stale AS stale, o.stale_reason AS stale_reason
		ORDER BY i`, map[string]any{"ids": chunkIDs, "testPath": testPathRegex, "chars": codeChars})
	return dropEmptyStale(rows), err
}
