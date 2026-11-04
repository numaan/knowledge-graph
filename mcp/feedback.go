package main

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"slices"
	"strings"
)

// Feedback from the REST API (POST /api/feedback) and the REST caller (rest.go).
//
// In main.go, after newREST:
//
//	api.Feedback = s.feedbackSink()
//	if oidc != nil { api.Identity = oidc.Caller }
//
// The access middleware (audit.go) then gives each REST tool call the caller's groups, mapped and
// bounded like an MCP token.

// graphFeedback stores feedback as a :Proposal (kind "feedback") for kg-review. The caller must
// see the node. The author is the caller of the context, never a name from the request body.
//
// Feedback on an answer of the Ask page has the pseudo id "answer:<question>". It has no node: the
// proposal gets subject "answer" and the question, without target_id, and kg-review handles it
// by hand (a reviewer gives the target or rejects it).
//
// Feedback needs an authenticated caller but not KG_OIDC_WRITE_SCOPE, which guards the write-back
// tools (writeback.go): the web UI does not ask for that scope.
//
// Errors are *httpError: 400 for bad input, 401 without a caller, 404 for a node that does not
// exist or is not visible (one status for both, like the message).
type graphFeedback struct{ s *Server }

func (s *Server) feedbackSink() FeedbackSink { return graphFeedback{s} }

var feedbackKind = regexp.MustCompile(`^[a-z_]{1,32}$`)

const (
	answerPrefix = "answer:"
	maxQuestion  = 1000
)

func (f graphFeedback) Submit(ctx context.Context, fb Feedback) error {
	if fb.NodeID == "" || !feedbackKind.MatchString(fb.Kind) {
		return fail(http.StatusBadRequest, "feedback needs node_id and a kind such as wrong, outdated or missing")
	}
	if err := limit("comment", fb.Comment, 0, maxReason); err != nil {
		return fail(http.StatusBadRequest, "%v", err)
	}
	if accessFrom(ctx) == nil {
		c := CallerFrom(ctx)
		if c == nil {
			c = fb.Caller // rest.go sets it from the request context, not from the body
		}
		if c == nil && f.s.requireCaller {
			return fail(http.StatusUnauthorized, "feedback needs an authenticated caller")
		}
		ctx = withAccess(ctx, f.s.restAccess(c))
	}
	props := map[string]any{"kind": "feedback", "feedback_kind": fb.Kind, "text": fb.Comment}
	if q, ok := strings.CutPrefix(fb.NodeID, answerPrefix); ok {
		if err := limit("question", q, 1, maxQuestion); err != nil {
			return fail(http.StatusBadRequest, "%v", err)
		}
		props["subject"], props["question"] = "answer", strings.TrimSpace(q)
	} else {
		if err := f.s.visibleNodes(ctx, fb.NodeID); err != nil {
			if nv := (*notVisibleError)(nil); errors.As(err, &nv) {
				return fail(http.StatusNotFound, "%v", err)
			}
			return err
		}
		props["target_id"] = fb.NodeID
	}
	a := accessFrom(ctx)
	_, _, _, err := f.s.backend().CreateProposal(ctx, proposalProps(props, a.User, a.Via, "rest"))
	return err
}

// restAccess is the Access of a REST caller. With OIDC its groups come from the token (oidc.Caller)
// and are mapped like MCP tokens; without OIDC the REST layer trusts nobody, so the caller gets
// the server groups, as in stdio mode.
func (s *Server) restAccess(c *Caller) *Access {
	user := "anonymous"
	if c != nil && c.Subject != "" {
		user = c.Subject
		if c.Name != "" {
			user = c.Name
		}
	}
	if !s.requireCaller {
		a := s.envAccess()
		a.User, a.Via = user, "rest"
		return a
	}
	if c == nil {
		return &Access{User: user, Groups: map[string]bool{}, Via: "rest"}
	}
	a := s.accessFor(user, c.Groups, c.Scopes) // scopes for canWrite (KG_OIDC_WRITE_SCOPE)
	a.Via = "rest"
	return a
}

// Caller is an IdentityFunc for the REST layer: it checks the bearer token like /mcp does
// (auth.RequireBearerToken): an invalid token gives 401, a token without every scope of
// KG_OIDC_SCOPES gives 403. The caller keeps the token's scopes for the write-back check.
func (o *OIDC) Caller(r *http.Request) (*Caller, error) {
	fields := strings.Fields(r.Header.Get("Authorization"))
	if len(fields) != 2 || !strings.EqualFold(fields[0], "bearer") {
		return nil, fmt.Errorf("no bearer token")
	}
	info, err := o.Verify(r.Context(), fields[1], r)
	if err != nil {
		return nil, err
	}
	for _, s := range o.Scopes {
		if !slices.Contains(info.Scopes, s) {
			return nil, fail(http.StatusForbidden, "insufficient scope")
		}
	}
	name, _ := info.Extra["user"].(string)
	groups, _ := info.Extra["groups"].([]string)
	return &Caller{Subject: info.UserID, Name: name, Groups: groups, Scopes: info.Scopes}, nil
}
