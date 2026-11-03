package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/neo4j/neo4j-go-driver/v5/neo4j"
)

// Write-back with review. Agents and people report wrong facts, record decisions and suggest
// links; each report becomes a :Proposal node with status "open". Nothing else in the graph
// changes: a reviewer runs kg-review (pipeline) to approve a proposal into a :Correction, which the
// pipeline applies. The write transaction runs one fixed statement that the guard below checks:
// it can only MERGE a :Proposal node and set its own properties. All other tools stay read-only.
//
// KG_WRITEBACK=off removes the three tools. KG_OIDC_WRITE_SCOPE makes them need an OAuth scope.

const (
	maxReason = 2000
	maxText   = 4000
	maxLinks  = 20
)

// proposalStatement is the only write statement of the server. Identical proposals of one author
// (same content_hash) are stored once.
const proposalStatement = `MERGE (p:Proposal {content_hash: $props.content_hash})
ON CREATE SET p = $props
RETURN p.id AS id, p.status AS status, p.id = $props.id AS created`

// proposalBackend looks up nodes and stores proposals; the Graph implements it, tests use a fake.
type proposalBackend interface {
	NodeAccess(ctx context.Context, id string) (found bool, repo string, acl any, err error)
	EdgeExists(ctx context.Context, src, edgeType, dst string) (bool, error)
	CreateProposal(ctx context.Context, props map[string]any) (id, status string, created bool, err error)
}

func (s *Server) backend() proposalBackend {
	if s.proposals != nil {
		return s.proposals
	}
	return s.graph
}

type WrongFactIn struct {
	NodeID   string `json:"node_id,omitempty" jsonschema:"id of the wrong node (symbol, concept, rule, requirement, ticket, section); or give the edge"`
	EdgeSrc  string `json:"edge_src,omitempty" jsonschema:"start node id of the wrong edge"`
	EdgeType string `json:"edge_type,omitempty" jsonschema:"type of the wrong edge, e.g. REALISES, IMPLEMENTED_BY, CALLS"`
	EdgeDst  string `json:"edge_dst,omitempty" jsonschema:"end node id of the wrong edge"`
	Reason   string `json:"reason" jsonschema:"what is wrong and why, with evidence (file, line, ticket)"`
}
type DecisionIn struct {
	Title string   `json:"title,omitempty" jsonschema:"short title of the decision"`
	Text  string   `json:"text" jsonschema:"the decision and its reason"`
	Links []string `json:"links,omitempty" jsonschema:"ids of the nodes the decision is about (symbols, concepts, tickets, requirements)"`
}
type LinkIn struct {
	Src      string `json:"src" jsonschema:"start node id"`
	EdgeType string `json:"edge_type" jsonschema:"edge type, e.g. REALISES, IMPLEMENTED_BY, MENTIONS"`
	Dst      string `json:"dst" jsonschema:"end node id"`
	Reason   string `json:"reason,omitempty" jsonschema:"why the link is correct"`
}
type ProposalOut struct {
	ID      string `json:"id"`
	Status  string `json:"status"`
	Created bool   `json:"created"`
	Note    string `json:"note"`
}

func (s *Server) registerWriteback(m *mcp.Server) {
	if env("KG_WRITEBACK", "on") == "off" {
		return
	}
	write := &mcp.ToolAnnotations{ReadOnlyHint: false, DestructiveHint: new(false), IdempotentHint: true}
	mcp.AddTool(m, &mcp.Tool{Name: "report_wrong_fact", Annotations: write, Description: "Report a wrong node or edge " +
		"in the graph (for example a wrong REALISES or IMPLEMENTED_BY link, or an outdated business rule). Creates a " +
		"proposal for human review; the graph does not change until a reviewer approves it."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in WrongFactIn) (*mcp.CallToolResult, ProposalOut, error) {
			if err := s.canWrite(ctx); err != nil {
				return nil, ProposalOut{}, err
			}
			if err := limit("reason", in.Reason, 1, maxReason); err != nil {
				return nil, ProposalOut{}, err
			}
			p := map[string]any{"kind": "wrong_fact", "text": in.Reason}
			switch edge := in.EdgeSrc != "" || in.EdgeType != "" || in.EdgeDst != ""; {
			case edge && in.NodeID != "":
				return nil, ProposalOut{}, fmt.Errorf("give node_id or the edge (edge_src, edge_type, edge_dst), not both")
			case edge:
				if in.EdgeSrc == "" || in.EdgeDst == "" || checkEdgeTypes([]string{in.EdgeType}) != nil || in.EdgeType == "" {
					return nil, ProposalOut{}, fmt.Errorf("an edge needs edge_src, edge_dst and a valid edge_type")
				}
				if err := s.visibleNodes(ctx, in.EdgeSrc, in.EdgeDst); err != nil {
					return nil, ProposalOut{}, err
				}
				if ok, err := s.backend().EdgeExists(ctx, in.EdgeSrc, in.EdgeType, in.EdgeDst); err != nil || !ok {
					return nil, ProposalOut{}, orErr(err, fmt.Errorf("no %s edge from %s to %s", in.EdgeType, in.EdgeSrc, in.EdgeDst))
				}
				p["target_id"], p["edge_type"], p["other_id"] = in.EdgeSrc, in.EdgeType, in.EdgeDst
			case in.NodeID != "":
				if err := s.visibleNodes(ctx, in.NodeID); err != nil {
					return nil, ProposalOut{}, err
				}
				p["target_id"] = in.NodeID
			default:
				return nil, ProposalOut{}, fmt.Errorf("give node_id or the edge (edge_src, edge_type, edge_dst)")
			}
			return s.propose(ctx, p, "mcp")
		})

	mcp.AddTool(m, &mcp.Tool{Name: "record_decision", Annotations: write, Description: "Record a design or business " +
		"decision (what was decided and why) and link it to the nodes it is about. Creates a proposal for human review."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in DecisionIn) (*mcp.CallToolResult, ProposalOut, error) {
			if err := s.canWrite(ctx); err != nil {
				return nil, ProposalOut{}, err
			}
			if err := orErr(limit("text", in.Text, 1, maxText), limit("title", in.Title, 0, 200)); err != nil {
				return nil, ProposalOut{}, err
			}
			links := slices.Compact(slices.Sorted(slices.Values(in.Links)))
			if len(links) > maxLinks {
				return nil, ProposalOut{}, fmt.Errorf("at most %d links", maxLinks)
			}
			if err := s.visibleNodes(ctx, links...); err != nil {
				return nil, ProposalOut{}, err
			}
			p := map[string]any{"kind": "decision", "title": in.Title, "text": in.Text, "links": links}
			if len(links) > 0 {
				p["target_id"] = links[0]
			}
			return s.propose(ctx, p, "mcp")
		})

	mcp.AddTool(m, &mcp.Tool{Name: "suggest_link", Annotations: write, Description: "Suggest a missing edge between two " +
		"nodes, for example a symbol that REALISES a concept or a requirement IMPLEMENTED_BY a symbol. Creates a " +
		"proposal for human review."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in LinkIn) (*mcp.CallToolResult, ProposalOut, error) {
			if err := s.canWrite(ctx); err != nil {
				return nil, ProposalOut{}, err
			}
			if in.Src == "" || in.Dst == "" || in.EdgeType == "" || checkEdgeTypes([]string{in.EdgeType}) != nil {
				return nil, ProposalOut{}, fmt.Errorf("give src, dst and a valid edge_type")
			}
			if err := limit("reason", in.Reason, 0, maxReason); err != nil {
				return nil, ProposalOut{}, err
			}
			if err := s.visibleNodes(ctx, in.Src, in.Dst); err != nil {
				return nil, ProposalOut{}, err
			}
			return s.propose(ctx, map[string]any{"kind": "link", "target_id": in.Src, "edge_type": in.EdgeType,
				"other_id": in.Dst, "text": in.Reason}, "mcp")
		})
}

func orErr(errs ...error) error {
	for _, e := range errs {
		if e != nil {
			return e
		}
	}
	return nil
}

func limit(name, v string, lo, hi int) error {
	n := utf8.RuneCountInString(strings.TrimSpace(v))
	if n < lo {
		return fmt.Errorf("%s is required", name)
	}
	if n > hi {
		return fmt.Errorf("%s is too long (%d characters, at most %d)", name, n, hi)
	}
	return nil
}

func (s *Server) canWrite(ctx context.Context) error {
	a := accessFrom(ctx)
	if a != nil && a.Via == "rest" && s.requireCaller {
		return nil // the REST layer checked the token; its Caller carries no scopes
	}
	if s.writeScope != "" && (a == nil || a.Via != "token" || !slices.Contains(a.Scopes, s.writeScope)) {
		return deny(ctx, "write-back needs the OAuth scope %s", s.writeScope)
	}
	return nil
}

// visibleNodes fails for nodes that do not exist or that the caller cannot see, with the same
// message, so that a caller cannot find hidden nodes through write-back.
func (s *Server) visibleNodes(ctx context.Context, ids ...string) error {
	for _, id := range ids {
		found, repo, acl, err := s.backend().NodeAccess(ctx, id)
		if err != nil {
			return err
		}
		if !found {
			return fmt.Errorf("node %s not found or not visible", id)
		}
		if !s.allowedRepo(ctx, repo) || !s.allowed(ctx, acl) {
			return deny(ctx, "node %s not found or not visible", id)
		}
	}
	return nil
}

func (s *Server) propose(ctx context.Context, p map[string]any, source string) (*mcp.CallToolResult, ProposalOut, error) {
	a := accessFrom(ctx)
	if a == nil {
		a = s.envAccess()
	}
	id, status, created, err := s.backend().CreateProposal(ctx, proposalProps(p, a.User, a.Via, source))
	if err != nil {
		return nil, ProposalOut{}, err
	}
	note := "proposal stored for review (kg-review list)"
	if !created {
		note = "the same proposal already exists"
	}
	return nil, ProposalOut{ID: id, Status: status, Created: created, Note: note}, nil
}

// proposalProps completes a proposal with id, author, time, status and content_hash. The hash
// covers what was proposed and by whom, not when, so a repeated call does not add a second node.
func proposalProps(p map[string]any, user, via, source string) map[string]any {
	out := map[string]any{}
	for k, v := range p {
		if s, ok := v.(string); ok && strings.TrimSpace(s) == "" {
			continue
		}
		out[k] = v
	}
	if out["links"] == nil {
		delete(out, "links")
	}
	out["author"], out["author_via"], out["source"] = user, via, source
	h, _ := json.Marshal([]any{out["kind"], out["target_id"], out["edge_type"], out["other_id"], out["links"],
		out["title"], out["text"], out["feedback_kind"], user})
	sum := sha256.Sum256(h)
	out["content_hash"] = hex.EncodeToString(sum[:10])
	b := make([]byte, 8)
	rand.Read(b)
	out["id"] = "proposal:" + hex.EncodeToString(b)
	out["created_at"] = time.Now().UTC().Format(time.RFC3339)
	out["status"] = "open"
	return out
}

// ---- Neo4j backend -------------------------------------------------------------------------------

func (g *Graph) NodeAccess(ctx context.Context, id string) (bool, string, any, error) {
	rows, err := g.Read(ctx, `CALL () { MATCH (n:Code {id: $id}) RETURN n UNION MATCH (n:Domain {id: $id}) RETURN n
		          UNION MATCH (n:Requirement {id: $id}) RETURN n UNION MATCH (n:Source {id: $id}) RETURN n }
		OPTIONAL MATCH (n)-[:PART_OF]->(d:Document)
		RETURN coalesce(n.repo, '') AS repo, coalesce(n.acl, d.acl) AS acl LIMIT 1`, map[string]any{"id": id})
	if err != nil || len(rows) == 0 {
		return false, "", nil, err
	}
	repo, _ := rows[0]["repo"].(string)
	return true, repo, rows[0]["acl"], nil
}

func (g *Graph) EdgeExists(ctx context.Context, src, edgeType, dst string) (bool, error) {
	if err := checkEdgeTypes([]string{edgeType}); err != nil {
		return false, err
	}
	rows, err := g.Read(ctx, `CALL () { MATCH (n:Code {id: $src}) RETURN n UNION MATCH (n:Domain {id: $src}) RETURN n
		          UNION MATCH (n:Requirement {id: $src}) RETURN n UNION MATCH (n:Source {id: $src}) RETURN n }
		MATCH (n)-[r:`+edgeType+`]->(m) WHERE m.id = $dst RETURN count(r) AS n`, map[string]any{"src": src, "dst": dst})
	if err != nil || len(rows) == 0 {
		return false, err
	}
	n, _ := rows[0]["n"].(int64)
	return n > 0, nil
}

func (g *Graph) CreateProposal(ctx context.Context, props map[string]any) (string, string, bool, error) {
	row, err := g.writeProposal(ctx, proposalStatement, map[string]any{"props": props})
	if err != nil {
		return "", "", false, err
	}
	id, _ := row["id"].(string)
	status, _ := row["status"].(string)
	created, _ := row["created"].(bool)
	return id, status, created, nil
}

// writeProposal is the only write transaction of the server. It refuses any statement that does
// more than create or merge a :Proposal node.
func (g *Graph) writeProposal(ctx context.Context, cypher string, params map[string]any) (map[string]any, error) {
	if err := checkProposalWrite(cypher); err != nil {
		return nil, err
	}
	session := g.driver.NewSession(ctx, neo4j.SessionConfig{AccessMode: neo4j.AccessModeWrite})
	defer session.Close(ctx)
	row, err := neo4j.ExecuteWrite(ctx, session, func(tx neo4j.ManagedTransaction) (map[string]any, error) {
		res, err := tx.Run(ctx, cypher, params)
		if err != nil {
			return nil, err
		}
		rec, err := res.Single(ctx)
		if err != nil {
			return nil, err
		}
		return rec.AsMap(), nil
	}, neo4j.WithTxTimeout(g.timeout))
	if err != nil {
		return nil, fmt.Errorf("proposal: %w", err)
	}
	return row, nil
}

// ---- guard for the write statement ---------------------------------------------------------------

var (
	proposalForbidden = regexp.MustCompile(`(?i)\b(MATCH|DELETE|DETACH|REMOVE|DROP|FOREACH|LOAD\s+CSV|CALL|ALTER|GRANT|DENY|REVOKE|UNWIND|START|STOP|USE)\b`)
	proposalCreate    = regexp.MustCompile(`(?i)\b(CREATE|MERGE)\b`)
	proposalNode      = regexp.MustCompile(`^\s*\(\s*p\s*:\s*Proposal\s*(\{[^()]*\})?\s*\)\s*($|[^-<,\s])`)
	proposalSet       = regexp.MustCompile(`(?is)\bSET\b(.*?)(\bRETURN\b|\bWITH\b|\bON\b|\bMERGE\b|\bCREATE\b|$)`)
	proposalAssign    = regexp.MustCompile(`^\s*p\s*(\.\s*[A-Za-z_][A-Za-z0-9_]*\s*)?(=|\+=)`)
)

// checkProposalWrite accepts a statement only when every CREATE or MERGE makes a single node
// (p:Proposal) without relationships, every SET changes p only, and it reads or deletes nothing.
func checkProposalWrite(cypher string) error {
	code := stringLits.ReplaceAllString(comments.ReplaceAllString(cypher, " "), "''")
	if m := proposalForbidden.FindString(code); m != "" {
		return fmt.Errorf("write-back: %q is not allowed", strings.ToUpper(m))
	}
	locs := proposalCreate.FindAllStringIndex(code, -1)
	if len(locs) == 0 {
		return fmt.Errorf("write-back: no CREATE or MERGE of a :Proposal")
	}
	for _, loc := range locs {
		if before := strings.TrimSpace(code[:loc[0]]); len(before) >= 2 && strings.EqualFold(before[len(before)-2:], "ON") {
			continue // MERGE ... ON CREATE SET
		}
		if !proposalNode.MatchString(code[loc[1]:]) {
			return fmt.Errorf("write-back: only a single (p:Proposal) node may be created")
		}
	}
	for _, m := range proposalSet.FindAllStringSubmatch(code, -1) {
		for _, item := range strings.Split(m[1], ",") {
			if !proposalAssign.MatchString(item) {
				return fmt.Errorf("write-back: SET may change the proposal p only")
			}
		}
	}
	return nil
}
