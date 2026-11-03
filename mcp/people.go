package main

import (
	"context"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// People tools: who knows a part of the code or a topic, and who owns it. People come from
// kg-people (:Person merged from git, Jira, Confluence, GitHub and the Backstage catalog);
// ownership from kg-catalog (catalog-info.yaml and CODEOWNERS).

type ExpertsIn struct {
	Topic string `json:"topic,omitempty" jsonschema:"question or topic in natural language, e.g. 'payment retries'"`
	Node  string `json:"node,omitempty" jsonschema:"symbol id, qualified name or file path; use instead of topic for one piece of code"`
	Repo  string `json:"repo,omitempty" jsonschema:"repository name"`
	Limit int    `json:"limit,omitempty" jsonschema:"number of people, 1-20 (default 5)"`
}
type OwnersIn struct {
	Node string `json:"node" jsonschema:"symbol id, qualified name, file path, or a catalog service, API, system or team name"`
	Repo string `json:"repo,omitempty" jsonschema:"repository name"`
}

// Evidence weights. Recent work counts more: each item halves its weight every halfLife.
// Ownership is a fact of the present, so it does not decay.
var expertWeights = map[string]float64{
	"commit": 1.0, "pr_author": 1.0, "ticket_assignee": 0.8, "review": 0.6, "doc_author": 0.5,
	"review_comment": 0.4, "ticket_reporter": 0.3, "code_owner": 1.5, "team_owner": 0.3,
}
var noDecay = map[string]bool{"code_owner": true, "team_owner": true}

const halfLife = 180 * 24 * time.Hour

func parseDate(v any) (time.Time, bool) {
	s, _ := v.(string)
	for _, layout := range []string{time.RFC3339, "2006-01-02T15:04:05.000-0700", "2006-01-02T15:04:05-0700", "2006-01-02"} {
		if t, err := time.Parse(layout, s); err == nil {
			return t, true
		}
	}
	return time.Time{}, false
}

// rankExperts scores people from evidence rows {person, name, bot, kind, item, title, url, date, acl}.
// Rows whose item the server may not see are dropped; one item counts once per person and kind.
func rankExperts(rows []map[string]any, now time.Time, allowed func(any) bool, limit int) []map[string]any {
	type person struct {
		id, name string
		score    float64
		kinds    map[string]int
		evidence []map[string]any
		last     time.Time
	}
	people := map[string]*person{}
	seen := map[string]bool{}
	for _, r := range rows {
		pid, _ := r["person"].(string)
		kind, _ := r["kind"].(string)
		item := fmt.Sprint(r["item"])
		w, ok := expertWeights[kind]
		if pid == "" || !ok || r["bot"] == true || !allowed(r["acl"]) || seen[pid+"|"+kind+"|"+item] {
			continue
		}
		seen[pid+"|"+kind+"|"+item] = true
		decay := 0.5 // undated work counts as about six months old
		date, dated := parseDate(r["date"])
		if noDecay[kind] {
			decay = 1
		} else if dated {
			decay = math.Pow(0.5, math.Max(0, now.Sub(date).Hours())/halfLife.Hours())
		}
		p := people[pid]
		if p == nil {
			name, _ := r["name"].(string)
			p = &person{id: pid, name: name, kinds: map[string]int{}}
			people[pid] = p
		}
		p.score += w * decay
		p.kinds[kind]++
		if dated && date.After(p.last) {
			p.last = date
		}
		p.evidence = append(p.evidence, map[string]any{"kind": kind, "item": r["item"], "title": r["title"],
			"url": r["url"], "date": r["date"], "weight": math.Round(w*decay*1000) / 1000})
	}
	list := make([]*person, 0, len(people))
	for _, p := range people {
		list = append(list, p)
	}
	sort.Slice(list, func(i, j int) bool {
		if list[i].score != list[j].score {
			return list[i].score > list[j].score
		}
		return list[i].id < list[j].id
	})
	out := []map[string]any{}
	for _, p := range list[:min(limit, len(list))] {
		sort.SliceStable(p.evidence, func(i, j int) bool { return p.evidence[i]["weight"].(float64) > p.evidence[j]["weight"].(float64) })
		row := map[string]any{"person": p.id, "name": p.name, "score": math.Round(p.score*1000) / 1000,
			"counts": p.kinds, "evidence": p.evidence[:min(5, len(p.evidence))]}
		if !p.last.IsZero() {
			row["last_activity"] = p.last.Format("2006-01-02")
		}
		out = append(out, row)
	}
	return out
}

// Evidence about code nodes (and the files that define them).
const codeEvidence = `
	UNWIND $ids AS id MATCH (x:Code {id: id}) WHERE $repos IS NULL OR x.repo IN $repos
	OPTIONAL MATCH (x)<-[:DEFINES]-(f:File)
	WITH collect(DISTINCT x) + collect(DISTINCT f) AS targets
	UNWIND targets AS t
	CALL (t) {
	  MATCH (p:Person)-[:AUTHORED]->(i:Commit)-[:MODIFIES]->(t) RETURN p, 'commit' AS kind, i, i.date AS date
	  UNION
	  MATCH (p:Person)-[:AUTHORED]->(i:PullRequest)-[:MODIFIES]->(t) RETURN p, 'pr_author' AS kind, i, coalesce(i.merged_at, i.updated) AS date
	  UNION
	  MATCH (p:Person)-[:REVIEWED]->(i:PullRequest)-[:MODIFIES]->(t) RETURN p, 'review' AS kind, i, coalesce(i.merged_at, i.updated) AS date
	  UNION
	  MATCH (p:Person)-[:AUTHORED]->(i:ReviewComment)-[:ON]->(t) RETURN p, 'review_comment' AS kind, i, i.date AS date
	  UNION
	  MATCH (p:Person)-[:ASSIGNED]->(i:Ticket)-[:MENTIONS]->(t) RETURN p, 'ticket_assignee' AS kind, i, i.updated AS date
	  UNION
	  MATCH (p:Person)-[:ASSIGNED]->(i:Ticket)<-[:REFERENCES]-(:Commit)-[:MODIFIES]->(t) RETURN p, 'ticket_assignee' AS kind, i, i.updated AS date
	  UNION
	  MATCH (p:Person)-[:OWNS]->(t) RETURN p, 'code_owner' AS kind, t AS i, null AS date
	  UNION
	  MATCH (p:Person)-[:MEMBER_OF]->(:Team)-[:OWNS]->(t) RETURN p, 'team_owner' AS kind, t AS i, null AS date
	}
	RETURN p.id AS person, p.name AS name, p.bot AS bot, kind, i.id AS item, coalesce(i.key + ': ', '') + coalesce(i.title, i.path) AS title,
	       i.url AS url, date, i.acl AS acl
	ORDER BY date DESC LIMIT 500`

// Evidence about source nodes (tickets, pull requests, reviews, documents) found for a topic.
const sourceEvidence = `
	UNWIND $ids AS id MATCH (n:Source {id: id})
	OPTIONAL MATCH (n)-[:PART_OF]->(d:Document)
	WITH coalesce(d, n) AS i
	MATCH (p:Person)-[r:AUTHORED|ASSIGNED|REPORTED|REVIEWED]->(i)
	RETURN p.id AS person, p.name AS name, p.bot AS bot,
	       CASE WHEN type(r) = 'ASSIGNED' THEN 'ticket_assignee' WHEN type(r) = 'REPORTED' THEN 'ticket_reporter'
	            WHEN type(r) = 'REVIEWED' THEN 'review' WHEN i:PullRequest THEN 'pr_author' WHEN i:Commit THEN 'commit'
	            WHEN i:Document THEN 'doc_author' ELSE 'review_comment' END AS kind,
	       i.id AS item, coalesce(i.key + ': ', '') + i.title AS title, i.url AS url,
	       coalesce(i.merged_at, i.updated, i.date) AS date, i.acl AS acl
	LIMIT 500`

func (s *Server) registerPeople(m *mcp.Server) {
	mcp.AddTool(m, &mcp.Tool{Name: "find_experts", Description: "People who know a piece of code or a topic, ranked " +
		"by recent commits, pull requests, reviews, review comments, ticket work and code ownership, each with " +
		"evidence. Give node (symbol or file) for code, or topic for a question. Use it to find a reviewer or " +
		"someone to ask."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in ExpertsIn) (*mcp.CallToolResult, Rows, error) {
			repo := s.repo(in.Repo)
			var codeIDs, sourceIDs []string
			switch {
			case in.Node != "":
				id, err := s.codeNode(ctx, in.Node, repo)
				if err != nil {
					return nil, Rows{}, err
				}
				codeIDs = []string{id}
			case in.Topic != "":
				hits, err := s.Candidates(ctx, repo, in.Topic, 8)
				if err != nil {
					return nil, Rows{}, err
				}
				chunkIDs := make([]string, len(hits))
				for i, h := range hits {
					chunkIDs[i] = h.ID
				}
				codeIDs = ids(must(s.graph.Read(ctx, `UNWIND $ids AS id MATCH (:Chunk {id: id})-[:PART_OF]->(o) RETURN DISTINCT o.id AS id`,
					map[string]any{"ids": chunkIDs})))
				if vec, err := s.emb.Query(ctx, in.Topic); err == nil {
					sourceIDs = ids(must(s.graph.Read(ctx, `CALL db.index.vector.queryNodes('source_embedding', 40, $v) YIELD node, score
						WHERE node:Ticket OR node:PullRequest OR node:Review OR node:ReviewComment OR node:Section
						RETURN node.id AS id ORDER BY score DESC LIMIT 10`, map[string]any{"v": vec})))
				}
			default:
				return nil, Rows{}, fmt.Errorf("give topic or node")
			}
			rows := must(s.graph.Read(ctx, codeEvidence, map[string]any{"ids": codeIDs}))
			if len(sourceIDs) > 0 {
				rows = append(rows, must(s.graph.Read(ctx, sourceEvidence, map[string]any{"ids": sourceIDs}))...)
			}
			out := rankExperts(rows, time.Now(), s.allowed, clamp(in.Limit, 1, 20, 5))
			note := ""
			if len(out) == 0 {
				note = "no people found; run kg-git-history, kg-github, kg-jira and then kg-people"
			}
			return nil, Rows{Results: out, Note: note}, nil
		})

	mcp.AddTool(m, &mcp.Tool{Name: "get_owners", Description: "Owners of code or of a catalog entity: the team and " +
		"people from CODEOWNERS, the service (Backstage catalog) that the code implements, its owning team, " +
		"system and team members. Give a symbol, a file path or a service, API, system or team name."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in OwnersIn) (*mcp.CallToolResult, Doc, error) {
			repo := s.repo(in.Repo)
			entity := one(must(s.graph.Read(ctx, `MATCH (n:Source) WHERE (n:Service OR n:API OR n:System OR n:Team)
				AND (n.id = $ref OR toLower(n.name) = toLower($ref)) AND ($repo = '' OR n.repo = $repo)
				RETURN n.id AS id, [l IN labels(n) WHERE l <> 'Source'][0] AS kind, n.acl AS acl LIMIT 1`,
				map[string]any{"ref": in.Node, "repo": repo})))
			if entity != nil && s.allowed(entity["acl"]) {
				return nil, Doc{Data: s.entityOwners(ctx, entity["id"].(string))}, nil
			}
			id, err := s.codeNode(ctx, in.Node, repo)
			if err != nil {
				return nil, Doc{}, err
			}
			p := map[string]any{"id": id}
			file := `MATCH (x:Code {id: $id}) OPTIONAL MATCH (x)<-[:DEFINES]-(f:File) WITH coalesce(f, x) AS file `
			data := map[string]any{"node": id,
				"code_owners": s.visible(must(s.graph.Read(ctx, file+`
					MATCH (o:Source)-[r:OWNS]->(file) WHERE o:Team OR o:Person
					RETURN [l IN labels(o) WHERE l <> 'Source'][0] AS kind, o.id AS id, o.title AS name,
					       r.via AS via, r.pattern AS pattern, o.acl AS acl ORDER BY kind DESC, name`, p))),
			}
			services := s.visible(must(s.graph.Read(ctx, file+`
				MATCH (svc:Service)-[:IMPLEMENTED_IN]->(t) WHERE t = file OR (t:Repository AND t.name = file.repo)
				RETURN svc.id AS id, svc.name AS name, svc.acl AS acl ORDER BY t = file DESC, name`, p)))
			list := []map[string]any{}
			for _, svc := range services {
				list = append(list, s.entityOwners(ctx, svc["id"].(string)))
			}
			data["services"] = list
			return nil, Doc{Data: data}, nil
		})
}

// codeNode resolves a file path, a node id or a symbol name to one code node id.
func (s *Server) codeNode(ctx context.Context, ref, repo string) (string, error) {
	if f := one(must(s.graph.Read(ctx, `MATCH (f:File) WHERE (f.path = $p OR f.id = $p) AND ($repo = '' OR f.repo = $repo)
		AND ($repos IS NULL OR f.repo IN $repos) RETURN f.id AS id LIMIT 1`, map[string]any{"p": ref, "repo": repo}))); f != nil {
		return f["id"].(string), nil
	}
	return s.resolve(ctx, ref, repo)
}

// visible drops rows the server may not see and removes their acl.
func (s *Server) visible(rows []map[string]any) []map[string]any {
	out := []map[string]any{}
	for _, r := range rows {
		if s.allowed(r["acl"]) {
			delete(r, "acl")
			out = append(out, r)
		}
	}
	return out
}

// entityOwners describes one catalog entity: owning teams and people, team members, system and code.
func (s *Server) entityOwners(ctx context.Context, id string) map[string]any {
	p := map[string]any{"id": id}
	e := one(must(s.graph.Read(ctx, `MATCH (n:Source {id: $id}) RETURN n.id AS id, [l IN labels(n) WHERE l <> 'Source'][0] AS kind,
		n.name AS name, n.title AS title, n.type AS type, n.lifecycle AS lifecycle, n.owner AS owner_ref,
		n.owner_source AS owner_source, n.repo AS repo, n.folder AS folder`, p)))
	if e == nil {
		return map[string]any{"id": id}
	}
	e["owners"] = s.visible(must(s.graph.Read(ctx, `MATCH (o:Source)-[r:OWNS]->(:Source {id: $id}) WHERE o:Team OR o:Person
		RETURN [l IN labels(o) WHERE l <> 'Source'][0] AS kind, o.id AS id, o.title AS name, r.via AS via, o.acl AS acl`, p)))
	e["members"] = s.visible(must(s.graph.Read(ctx, `MATCH (n:Source {id: $id}) OPTIONAL MATCH (t:Team)-[:OWNS]->(n)
		WITH n, collect(t) AS owners WITH [x IN owners + [n] WHERE x:Team] AS teams UNWIND teams AS t
		MATCH (p:Person)-[:MEMBER_OF]->(t) RETURN DISTINCT p.id AS id, p.name AS name, t.title AS team, p.acl AS acl
		ORDER BY team, name LIMIT 50`, p)))
	e["part_of"] = s.visible(must(s.graph.Read(ctx, `MATCH (:Source {id: $id})-[:PART_OF]->(x:Source)
		RETURN [l IN labels(x) WHERE l <> 'Source'][0] AS kind, x.id AS id, x.title AS name, x.acl AS acl`, p)))
	e["apis"] = s.visible(must(s.graph.Read(ctx, `MATCH (:Source {id: $id})-[r:PROVIDES_API|CONSUMES_API]->(a:API)
		RETURN toLower(type(r)) AS relation, a.id AS id, a.name AS name, a.acl AS acl ORDER BY relation, name`, p)))
	code := one(must(s.graph.Read(ctx, `MATCH (:Source {id: $id})-[:IMPLEMENTED_IN]->(c:Code)
		WHERE $repos IS NULL OR c.repo IN $repos
		RETURN count(c) AS files, collect(coalesce(c.path, c.name))[..10] AS sample`, p)))
	if code != nil && code["files"] != int64(0) {
		e["implemented_in"] = code
	}
	if folder, _ := e["folder"].(string); folder == "" {
		delete(e, "folder")
	}
	for k, v := range e {
		if v == nil {
			delete(e, k)
		}
	}
	if strings.HasPrefix(fmt.Sprint(e["owner_ref"]), "@") {
		e["owner_source"] = "codeowners"
	}
	return e
}
