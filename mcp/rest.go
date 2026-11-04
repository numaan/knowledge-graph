package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// REST layer for the web UI, on the same HTTP listener as /mcp.
//
//	GET  /api/tools              names, descriptions and input schemas of all registered tools
//	POST /api/tools/{name}       call one tool; body = tool arguments; response = structured result
//	GET  /api/node/{id}          one node (labels, properties) and its neighbours (get_neighbors)
//	GET  /api/requirements       checked requirements with verdict counts and evidence (?repo=)
//	POST /api/feedback           {node_id, kind, comment} -> 202, through a FeedbackSink
//
// Tools run in-process through an in-memory MCP session on the same *mcp.Server, so every tool
// that any file registers is available here with the same ACL groups and $repos scope.

// Caller is the person or agent behind a REST request.
type Caller struct {
	Subject string   `json:"subject"`
	Name    string   `json:"name,omitempty"`
	Email   string   `json:"email,omitempty"`
	Groups  []string `json:"groups,omitempty"`
	Scopes  []string `json:"scopes,omitempty"` // OAuth scopes of the token (OIDC.Caller)
}

type callerKey struct{}

func WithCaller(ctx context.Context, c *Caller) context.Context {
	return context.WithValue(ctx, callerKey{}, c)
}

// CallerFrom returns the caller of a REST request, also inside tool handlers; nil for MCP clients.
func CallerFrom(ctx context.Context) *Caller {
	c, _ := ctx.Value(callerKey{}).(*Caller)
	return c
}

// IdentityFunc reads the caller from a request. The default trusts nobody: every request is the
// local user. An OIDC implementation replaces it (REST.Identity) and may return an error for 401,
// or an *httpError with status 403 for a valid token that lacks a required scope.
type IdentityFunc func(r *http.Request) (*Caller, error)

func localIdentity(*http.Request) (*Caller, error) { return &Caller{Subject: "local"}, nil }

// Feedback from a human on an answer or an entity page.
type Feedback struct {
	NodeID  string    `json:"node_id"`
	Kind    string    `json:"kind"` // wrong, outdated, helpful, missing
	Comment string    `json:"comment,omitempty"`
	Caller  *Caller   `json:"caller,omitempty"`
	At      time.Time `json:"at"`
}

var feedbackKinds = map[string]bool{"wrong": true, "outdated": true, "helpful": true, "missing": true}

// FeedbackSink stores feedback. The default only logs it.
type FeedbackSink interface {
	Submit(ctx context.Context, f Feedback) error
}

type logSink struct{}

func (logSink) Submit(_ context.Context, f Feedback) error {
	who := "unknown"
	if f.Caller != nil {
		who = f.Caller.Subject
	}
	log.Printf("feedback: %s on %q by %s: %.200q", f.Kind, f.NodeID, who, f.Comment)
	return nil
}

type REST struct {
	Identity IdentityFunc
	Feedback FeedbackSink
	Timeout  time.Duration // per tool call
	// node reads one node by id; nil means not found or not visible. Tests replace it.
	node func(ctx context.Context, id string) (map[string]any, error)
	reqs func(ctx context.Context, repo string) ([]map[string]any, error)
	// access puts the caller's ACL groups into the context for node and reqs, which read the
	// graph directly (tool calls get them from the access middleware). nil in tests.
	access  func(ctx context.Context, c *Caller) context.Context
	session *mcp.ClientSession
	server  *mcp.ServerSession
}

const callerMeta = "kg/caller"

// newREST connects an in-memory client to m. Call it after all tools are registered.
func newREST(ctx context.Context, s *Server, m *mcp.Server) (*REST, error) {
	api := &REST{Identity: localIdentity, Feedback: logSink{}, Timeout: 120 * time.Second}
	if s != nil {
		api.access = func(ctx context.Context, c *Caller) context.Context { return withAccess(ctx, s.restAccess(c)) }
		if s.graph != nil {
			api.node, api.reqs = s.nodeByID, s.requirements
		}
	}
	// The caller travels as _meta on the call; only this session may set it, so MCP clients cannot spoof it.
	m.AddReceivingMiddleware(func(next mcp.MethodHandler) mcp.MethodHandler {
		return func(ctx context.Context, method string, req mcp.Request) (mcp.Result, error) {
			if method == "tools/call" && api.server != nil && req.GetSession() == api.server {
				if raw, ok := req.GetParams().GetMeta()[callerMeta]; ok {
					b, _ := json.Marshal(raw)
					var c Caller
					if json.Unmarshal(b, &c) == nil {
						ctx = WithCaller(ctx, &c)
					}
				}
			}
			return next(ctx, method, req)
		}
	})
	st, ct := mcp.NewInMemoryTransports()
	ss, err := m.Connect(ctx, st, nil)
	if err != nil {
		return nil, err
	}
	api.server = ss
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "kg-rest", Version: version}, nil).Connect(ctx, ct, nil)
	if err != nil {
		return nil, err
	}
	api.session = cs
	return api, nil
}

func (api *REST) Close() error { return api.session.Close() }

func (api *REST) Mount(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/tools", api.with(api.listTools))
	mux.HandleFunc("POST /api/tools/{name}", api.with(api.callTool))
	mux.HandleFunc("GET /api/node/{id...}", api.with(api.getNode))
	mux.HandleFunc("GET /api/requirements", api.with(api.getRequirements))
	mux.HandleFunc("POST /api/feedback", api.with(api.postFeedback))
}

type httpError struct {
	status int
	msg    string
}

func (e *httpError) Error() string { return e.msg }

func fail(status int, format string, a ...any) error {
	return &httpError{status, fmt.Sprintf(format, a...)}
}

// with resolves the caller, runs h and writes its value or error as JSON.
func (api *REST) with(h func(w http.ResponseWriter, r *http.Request) (int, any, error)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		caller, err := api.Identity(r)
		if err != nil || caller == nil {
			status, msg := http.StatusUnauthorized, "unauthorized"
			if he := (*httpError)(nil); errors.As(err, &he) && he.status == http.StatusForbidden {
				status, msg = he.status, he.msg
			}
			writeJSON(w, status, map[string]string{"error": msg})
			return
		}
		ctx := WithCaller(r.Context(), caller)
		if api.access != nil {
			ctx = api.access(ctx, caller)
		}
		r = r.WithContext(ctx)
		r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
		status, v, err := h(w, r)
		if err != nil {
			var he *httpError
			if !errors.As(err, &he) {
				he = &httpError{http.StatusInternalServerError, err.Error()}
			}
			writeJSON(w, he.status, map[string]string{"error": he.msg})
			return
		}
		writeJSON(w, status, v)
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

type toolInfo struct {
	Name        string `json:"name"`
	Title       string `json:"title,omitempty"`
	Description string `json:"description"`
	InputSchema any    `json:"input_schema"`
}

func (api *REST) listTools(_ http.ResponseWriter, r *http.Request) (int, any, error) {
	out := []toolInfo{}
	for t, err := range api.session.Tools(r.Context(), nil) {
		if err != nil {
			return 0, nil, err
		}
		out = append(out, toolInfo{t.Name, t.Title, t.Description, t.InputSchema})
	}
	return http.StatusOK, map[string]any{"tools": out}, nil
}

// call runs one tool as the caller of ctx and returns its structured result.
func (api *REST) call(ctx context.Context, name string, args map[string]any) (any, error) {
	ctx, cancel := context.WithTimeout(ctx, api.Timeout)
	defer cancel()
	params := &mcp.CallToolParams{Name: name, Arguments: args}
	if c := CallerFrom(ctx); c != nil {
		params.Meta = mcp.Meta{callerMeta: c}
	}
	res, err := api.session.CallTool(ctx, params)
	if err != nil {
		if strings.Contains(err.Error(), "unknown tool") {
			return nil, fail(http.StatusNotFound, "tool %q is not configured on this server", name)
		}
		if ctx.Err() != nil {
			return nil, fail(http.StatusGatewayTimeout, "tool %q timed out", name)
		}
		return nil, fail(http.StatusBadRequest, "%v", err)
	}
	if res.IsError {
		return nil, fail(http.StatusUnprocessableEntity, "%s", toolText(res))
	}
	if res.StructuredContent != nil {
		return res.StructuredContent, nil
	}
	return map[string]any{"text": toolText(res)}, nil
}

func toolText(res *mcp.CallToolResult) string {
	var b strings.Builder
	for _, c := range res.Content {
		if t, ok := c.(*mcp.TextContent); ok {
			b.WriteString(t.Text)
		}
	}
	return b.String()
}

func (api *REST) callTool(_ http.ResponseWriter, r *http.Request) (int, any, error) {
	args := map[string]any{}
	if err := json.NewDecoder(r.Body).Decode(&args); err != nil && !errors.Is(err, io.EOF) {
		return 0, nil, fail(http.StatusBadRequest, "body must be a JSON object of tool arguments: %v", err)
	}
	v, err := api.call(r.Context(), r.PathValue("name"), args)
	return http.StatusOK, v, err
}

func (api *REST) getNode(_ http.ResponseWriter, r *http.Request) (int, any, error) {
	id := r.PathValue("id")
	if id == "" || api.node == nil {
		return 0, nil, fail(http.StatusNotFound, "node %q not found", id)
	}
	node, err := api.node(r.Context(), id)
	if err != nil {
		return 0, nil, err
	}
	if node == nil {
		return 0, nil, fail(http.StatusNotFound, "node %q not found or not visible", id)
	}
	limit := 100
	nb, err := api.call(r.Context(), "get_neighbors", map[string]any{"node_id": id, "limit": limit})
	if err != nil {
		return 0, nil, err
	}
	return http.StatusOK, map[string]any{"node": node, "neighbors": nb}, nil
}

func (api *REST) getRequirements(_ http.ResponseWriter, r *http.Request) (int, any, error) {
	if api.reqs == nil {
		return 0, nil, fail(http.StatusNotFound, "requirements are not available")
	}
	rows, err := api.reqs(r.Context(), r.URL.Query().Get("repo"))
	if err != nil {
		return 0, nil, err
	}
	counts := map[string]int{"supported": 0, "contradicted": 0, "unresolved": 0}
	for _, row := range rows {
		v, _ := row["verdict"].(string)
		if v == "" {
			v = "unresolved"
		}
		counts[v]++
	}
	return http.StatusOK, map[string]any{"counts": counts, "total": len(rows), "requirements": rows}, nil
}

func (api *REST) postFeedback(_ http.ResponseWriter, r *http.Request) (int, any, error) {
	var f Feedback
	if err := json.NewDecoder(r.Body).Decode(&f); err != nil {
		return 0, nil, fail(http.StatusBadRequest, "invalid JSON: %v", err)
	}
	f.NodeID, f.Comment = strings.TrimSpace(f.NodeID), strings.TrimSpace(f.Comment)
	switch {
	case f.NodeID == "" || len(f.NodeID) > 1000:
		return 0, nil, fail(http.StatusBadRequest, "node_id is required")
	case !feedbackKinds[f.Kind]:
		return 0, nil, fail(http.StatusBadRequest, "kind must be wrong, outdated, helpful or missing")
	case utf8.RuneCountInString(f.Comment) > 4000:
		return 0, nil, fail(http.StatusBadRequest, "comment is longer than 4000 characters")
	}
	f.Caller, f.At = CallerFrom(r.Context()), time.Now().UTC()
	if err := api.Feedback.Submit(r.Context(), f); err != nil {
		return 0, nil, err
	}
	return http.StatusAccepted, map[string]string{"status": "accepted"}, nil
}

// nodeByID reads one node with the same scope rules as the tools: $repos for code and ACL for sources.
func (s *Server) nodeByID(ctx context.Context, id string) (map[string]any, error) {
	rows, err := s.graph.Read(ctx, `
		CALL () { MATCH (n:Code {id: $id}) RETURN n UNION MATCH (n:Domain {id: $id}) RETURN n
		          UNION MATCH (n:Requirement {id: $id}) RETURN n UNION MATCH (n:Source {id: $id}) RETURN n }
		WITH n WHERE $repos IS NULL OR n.repo IS NULL OR n.repo IN $repos
		OPTIONAL MATCH (n)-[:PART_OF]->(d:Document)
		RETURN n AS node, d.acl AS doc_acl LIMIT 1`, map[string]any{"id": id})
	row := one(rows)
	if err != nil || row == nil {
		return nil, err
	}
	node, _ := row["node"].(map[string]any)
	props, _ := node["properties"].(map[string]any)
	acl := props["acl"]
	if acl == nil {
		acl = row["doc_acl"]
	}
	if !s.allowed(ctx, acl) {
		return nil, nil
	}
	delete(props, "acl")
	return node, nil
}

// requirements lists checked requirements in the $repos scope, with their evidence and test count.
func (s *Server) requirements(ctx context.Context, repo string) ([]map[string]any, error) {
	return s.graph.Read(ctx, `
		MATCH (r:Requirement) WHERE ($repo = '' OR r.repo = $repo) AND ($repos IS NULL OR r.repo IN $repos)
		CALL (r) { OPTIONAL MATCH (r)-[e:IMPLEMENTED_BY|CONTRADICTED_BY]->(x:Symbol)
		           RETURN collect(CASE WHEN x IS NULL THEN NULL ELSE {link: type(e), id: x.id, symbol: x.qualname,
		                  path: x.path, confidence: e.confidence} END)[..10] AS evidence }
		CALL (r) { OPTIONAL MATCH (r)-[:VALIDATED_BY]->(t:Symbol) RETURN count(t) AS tests }
		RETURN r.id AS id, r.ref AS ref, r.statement AS statement, r.type AS type, r.source AS source,
		       r.section AS section, r.repo AS repo, r.verdict AS verdict, r.confidence AS confidence,
		       r.gaps AS gaps, r.checked_at AS checked_at, evidence, tests
		ORDER BY source, ref`, map[string]any{"repo": repo})
}
