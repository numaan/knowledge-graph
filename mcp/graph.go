package main

import (
	"context"
	"fmt"
	"time"

	"github.com/neo4j/neo4j-go-driver/v5/neo4j"
	"github.com/neo4j/neo4j-go-driver/v5/neo4j/dbtype"
	"go.opentelemetry.io/otel/attribute"
)

// Graph runs read-only Cypher. Every query goes through a read transaction with a timeout,
// so the server cannot change the graph even if a query slips past the Cypher guard.
type Graph struct {
	driver  neo4j.DriverWithContext
	timeout time.Duration
	maxRows int
	fixed   map[string]any // parameters added to every query (the ACL scope $repos); callers cannot override them
}

func NewGraph(uri, user, password string) (*Graph, error) {
	d, err := neo4j.NewDriverWithContext(uri, neo4j.BasicAuth(user, password, ""))
	if err != nil {
		return nil, err
	}
	return &Graph{driver: d, timeout: 15 * time.Second, maxRows: 500, fixed: map[string]any{"repos": nil}}, nil
}

func (g *Graph) Close(ctx context.Context) error { return g.driver.Close(ctx) }

func (g *Graph) Verify(ctx context.Context) error { return g.driver.VerifyConnectivity(ctx) }

// Read returns at most maxRows rows as JSON-ready maps.
func (g *Graph) Read(ctx context.Context, cypher string, params map[string]any) (_ []map[string]any, err error) {
	ctx, span, start := childSpan(ctx, "neo4j.read", attribute.String("db.system.name", "neo4j"),
		attribute.String("db.query.text", truncate(cypher, 2000)))
	defer func() { endSpan(span, start, querySeconds, err) }()
	merged := make(map[string]any, len(params)+len(g.fixed))
	for k, v := range params {
		merged[k] = v
	}
	for k, v := range g.fixed {
		merged[k] = v
	}
	params = merged
	session := g.driver.NewSession(ctx, neo4j.SessionConfig{AccessMode: neo4j.AccessModeRead})
	defer session.Close(ctx)
	rows, err := neo4j.ExecuteRead(ctx, session, func(tx neo4j.ManagedTransaction) ([]map[string]any, error) {
		res, err := tx.Run(ctx, cypher, params)
		if err != nil {
			return nil, err
		}
		out := []map[string]any{} // never nil: tool outputs must serialise as [] not null
		for res.Next(ctx) {
			if len(out) == g.maxRows {
				break
			}
			row := map[string]any{}
			for k, v := range res.Record().AsMap() {
				row[k] = plain(v)
			}
			out = append(out, row)
		}
		return out, res.Err()
	}, neo4j.WithTxTimeout(g.timeout))
	if err != nil {
		return nil, fmt.Errorf("cypher: %w", err)
	}
	return rows, nil
}

// plain turns driver graph types into maps so they serialise as readable JSON.
func plain(v any) any {
	switch x := v.(type) {
	case dbtype.Node:
		return map[string]any{"labels": x.Labels, "properties": dropVectors(x.Props)}
	case dbtype.Relationship:
		return map[string]any{"type": x.Type, "properties": x.Props}
	case dbtype.Path:
		nodes := make([]any, len(x.Nodes))
		for i, n := range x.Nodes {
			nodes[i] = plain(n)
		}
		rels := make([]any, len(x.Relationships))
		for i, r := range x.Relationships {
			rels[i] = plain(r)
		}
		return map[string]any{"nodes": nodes, "relationships": rels}
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = plain(e)
		}
		return out
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			out[k] = plain(e)
		}
		return dropVectors(out)
	default:
		return v
	}
}

// Embeddings are large and useless to an agent; never return them.
func dropVectors(props map[string]any) map[string]any {
	delete(props, "embedding")
	delete(props, "embedding_hash")
	return props
}
