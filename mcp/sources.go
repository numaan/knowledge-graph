package main

import (
	"context"
	"fmt"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Tools over non-code sources: Jira tickets, git history and documents (all :Source nodes).

type TicketIn struct {
	Key string `json:"key" jsonschema:"Jira issue key, e.g. PREC-123"`
}
type KnowledgeIn struct {
	Query string `json:"query" jsonschema:"question in natural language"`
	Kind  string `json:"kind,omitempty" jsonschema:"Ticket, Section (documents and wiki pages), PullRequest (pull requests and reviews) or empty for all"`
	K     int    `json:"k,omitempty" jsonschema:"number of results, 1-20 (default 8)"`
}
type HistoryIn struct {
	Symbol string `json:"symbol" jsonschema:"symbol id, qualified name or file path"`
	Repo   string `json:"repo,omitempty" jsonschema:"repository name"`
	Limit  int    `json:"limit,omitempty" jsonschema:"maximum commits, 1-50 (default 15)"`
}

func (s *Server) registerSources(m *mcp.Server) {
	mcp.AddTool(m, &mcp.Tool{Name: "get_context_for_ticket", Description: "Everything needed to work on a Jira " +
		"ticket: its text and acceptance criteria, epic, sub-tasks, linked issues, release, the commits that " +
		"reference it and the code they changed, code named in the ticket, checked requirements and the code that " +
		"best matches the ticket text. Use it first when a task names a ticket."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in TicketIn) (*mcp.CallToolResult, Doc, error) {
			p := map[string]any{"id": "jira:" + strings.ToUpper(strings.TrimSpace(in.Key))}
			t := one(must(s.graph.Read(ctx, `MATCH (t:Ticket {id: $id})
				RETURN t.key AS key, t.type AS type, t.status AS status, t.priority AS priority, t.title AS title,
				       coalesce(t.text_masked, t.text) AS text, t.labels AS labels, t.components AS components, t.url AS url,
				       t.created AS created, t.updated AS updated, t.source_updated_at AS source_updated_at,
				       t.ingested_at AS ingested_at, t.author AS author, t.acl AS acl, t.stale AS stale, t.stale_reason AS stale_reason`, p)))
			if t == nil {
				return nil, Doc{}, fmt.Errorf("ticket %s is not in the graph (run kg-jira for its project)", in.Key)
			}
			if !s.allowed(ctx, t["acl"]) {
				return nil, Doc{}, deny(ctx, "ticket %s is not visible to this server", in.Key)
			}
			delete(t, "acl")
			dropEmptyStale([]map[string]any{t})
			data := map[string]any{"ticket": t,
				"parent": one(must(s.graph.Read(ctx, `MATCH (:Ticket {id: $id})-[:CHILD_OF]->(p:Ticket)
					RETURN p.key AS key, p.type AS type, p.title AS title, p.status AS status`, p))),
				"children": must(s.graph.Read(ctx, `MATCH (c:Ticket)-[:CHILD_OF]->(:Ticket {id: $id})
					RETURN c.key AS key, c.type AS type, c.title AS title, c.status AS status ORDER BY key`, p)),
				"links": must(s.graph.Read(ctx, `MATCH (t:Ticket {id: $id})-[l:LINKS]-(o:Ticket)
					RETURN CASE WHEN startNode(l) = t THEN l.type ELSE 'inward: ' + l.type END AS link,
					       o.key AS key, o.title AS title, o.status AS status`, p)),
				"releases": must(s.graph.Read(ctx, `MATCH (:Ticket {id: $id})-[:IN_RELEASE]->(r:Release)
					RETURN r.name AS name, r.released AS released, r.release_date AS date, r.url AS url`, p)),
				"commits": must(s.graph.Read(ctx, `MATCH (c:Commit)-[:REFERENCES]->(:Ticket {id: $id})
					WHERE $repos IS NULL OR c.repo IN $repos
					OPTIONAL MATCH (c)-[:MODIFIES]->(x:Code)
					WITH c, collect(DISTINCT coalesce(x.qualname, x.path))[..20] AS changed
					RETURN c.sha AS sha, c.title AS title, c.author AS author, c.date AS date, c.repo AS repo, c.url AS url,
					       c.ingested_at AS ingested_at, changed
					ORDER BY c.date DESC LIMIT 20`, p)),
				"mentioned_code": must(s.graph.Read(ctx, `MATCH (:Ticket {id: $id})-[m:MENTIONS]->(x:Code)
					WHERE $repos IS NULL OR x.repo IN $repos
					RETURN x.id AS id, coalesce(x.qualname, x.path) AS name, x.path AS path, m.term AS term`, p)),
				"requirements": must(s.graph.Read(ctx, `MATCH (:Ticket {id: $id})-[:SPECIFIES]->(r:Requirement)
					RETURN r.ref AS ref, r.statement AS statement, r.verdict AS verdict, r.gaps AS gaps`, p)),
			}
			text, _ := t["text"].(string)
			query := fmt.Sprint(t["title"]) + "\n" + text[:min(len(text), 2000)]
			if hits, err := s.Candidates(ctx, s.defaultRepo, query, 5); err == nil {
				ids := make([]string, len(hits))
				for i, h := range hits {
					ids[i] = h.ID
				}
				data["related_code"] = must(s.Expand(ctx, ids, 600))
			}
			return nil, Doc{Data: data}, nil
		})

	mcp.AddTool(m, &mcp.Tool{Name: "search_knowledge", Description: "Search Jira tickets, document or wiki " +
		"sections (specifications, ADRs, runbooks) and pull requests with their reviews by meaning and keywords. Use it for why and what questions; " +
		"use search_graph for where-in-the-code questions."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in KnowledgeIn) (*mcp.CallToolResult, Rows, error) {
			if in.Kind != "" && in.Kind != "Ticket" && in.Kind != "Section" && in.Kind != "PullRequest" {
				return nil, Rows{}, fmt.Errorf("kind must be Ticket, Section, PullRequest or empty")
			}
			vec, err := s.emb.Query(ctx, in.Query)
			if err != nil {
				return nil, Rows{}, err
			}
			p := map[string]any{"v": vec, "q": lucene(in.Query), "kind": in.Kind}
			// Reviews and review comments are found with their pull request (kind PullRequest).
			filter := `($kind = '' OR $kind IN labels(node) OR ($kind = 'PullRequest' AND (node:Review OR node:ReviewComment))) AND
				(node:Ticket OR node:Section OR node:PullRequest OR node:Review OR node:ReviewComment)`
			byVector, err := s.graph.Read(ctx, `CALL db.index.vector.queryNodes('source_embedding', 60, $v) YIELD node, score
				WHERE `+filter+` RETURN node.id AS id ORDER BY score DESC LIMIT 30`, p)
			if err != nil {
				return nil, Rows{}, err
			}
			byText, err := s.graph.Read(ctx, `CALL db.index.fulltext.queryNodes('source_text', $q, {limit: 60}) YIELD node, score
				WHERE `+filter+` RETURN node.id AS id ORDER BY score DESC LIMIT 30`, p)
			if err != nil {
				return nil, Rows{}, err
			}
			k := clamp(in.K, 1, 20, 8)
			byReview := must(s.graph.Read(ctx, `CALL db.index.fulltext.queryNodes('review_text', $q, {limit: 60}) YIELD node, score
				WHERE `+filter+` RETURN node.id AS id ORDER BY score DESC LIMIT 30`, p)) // no index before kg-github
			fused := rrf(ids(byVector), ids(byText), ids(byReview))
			fused = fused[:min(len(fused), 3*k)] // spare rows for the ACL filter and collapsed duplicates
			idList := make([]string, len(fused))
			for i, f := range fused {
				idList[i] = f.ID
			}
			rows, err := s.graph.Read(ctx, `UNWIND range(0, size($ids) - 1) AS i MATCH (n:Source {id: $ids[i]})
				OPTIONAL MATCH (n)-[:PART_OF]->(d:Document)
				OPTIONAL MATCH (n)-[:DUPLICATE_OF]->(c:Source)
				RETURN n.id AS id, [l IN labels(n) WHERE l <> 'Source'][0] AS kind, n.key AS key, n.type AS type,
				       n.status AS status, coalesce(d.title + ' > ', '') + n.title AS title, coalesce(n.url, d.url) AS url,
				       coalesce(n.source_updated_at, d.source_updated_at) AS source_updated_at,
				       coalesce(n.ingested_at, d.ingested_at) AS ingested_at, coalesce(n.author, d.author) AS author,
				       left(coalesce(n.text_masked, n.text), 1200) AS text, coalesce(n.acl, d.acl) AS acl,
				       coalesce(n.stale, d.stale) AS stale, coalesce(n.stale_reason, d.stale_reason) AS stale_reason,
				       coalesce(c.id, n.id) AS canonical ORDER BY i`, map[string]any{"ids": idList})
			if err != nil {
				return nil, Rows{}, err
			}
			out := []map[string]any{}
			for _, r := range rows {
				if s.allowed(ctx, r["acl"]) {
					delete(r, "acl")
					out = append(out, r)
				}
			}
			return nil, Rows{Results: collapseDuplicates(dropEmptyStale(out), k)}, nil
		})

	mcp.AddTool(m, &mcp.Tool{Name: "get_change_history", Description: "Recent commits that changed a symbol or " +
		"file: who, when, why (message and pull request description) and the tickets they reference. Use it to understand why code is the way it is."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in HistoryIn) (*mcp.CallToolResult, Rows, error) {
			repo := s.repo(in.Repo)
			id := ""
			if f := one(must(s.graph.Read(ctx, `MATCH (f:File) WHERE f.path = $p AND ($repo = '' OR f.repo = $repo) AND ($repos IS NULL OR f.repo IN $repos) RETURN f.id AS id LIMIT 1`,
				map[string]any{"p": in.Symbol, "repo": repo}))); f != nil {
				id = f["id"].(string)
			} else {
				var err error
				if id, err = s.resolve(ctx, in.Symbol, repo); err != nil {
					return nil, Rows{}, err
				}
			}
			rows, err := s.graph.Read(ctx, `
				MATCH (x:Code {id: $id})
				OPTIONAL MATCH (x)<-[:DEFINES]-(f:File)
				WITH x, coalesce(f, x) AS file
				MATCH (c:Commit)-[m:MODIFIES]->(t) WHERE t = x OR t = file
				WITH c, max(CASE WHEN t = x THEN 1 ELSE 0 END) AS exact, collect(m.change)[0] AS change
				OPTIONAL MATCH (c)-[:REFERENCES]->(k:Ticket)
				WITH c, exact, change, collect(k.key + ': ' + k.title) AS tickets
				OPTIONAL MATCH (pr:PullRequest)-[:CONTAINS]->(c) WHERE $repos IS NULL OR pr.repo IN $repos
				RETURN c.sha AS sha, c.date AS date, c.author AS author, c.title AS title, left(c.text, 600) AS message, c.url AS url,
				       change, exact = 1 AS touched_symbol, tickets,
				       collect(pr.key + ': ' + pr.title + ' (' + coalesce(pr.url, '') + ')' + coalesce('
' + left(pr.text, 400), '')) AS pull_requests
				ORDER BY date DESC LIMIT $limit`, map[string]any{"id": id, "limit": clamp(in.Limit, 1, 50, 15)})
			note := ""
			if err == nil && len(rows) == 0 {
				note = "no commits found; run kg-git-history for this repository"
			}
			return nil, Rows{Results: rows, Note: note}, err
		})
}
