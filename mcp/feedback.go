package main

import (
	"context"
	"fmt"
	"net/http"
	"regexp"
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
type graphFeedback struct{ s *Server }

func (s *Server) feedbackSink() FeedbackSink { return graphFeedback{s} }

var feedbackKind = regexp.MustCompile(`^[a-z_]{1,32}$`)

func (f graphFeedback) Submit(ctx context.Context, fb Feedback) error {
	if fb.NodeID == "" || !feedbackKind.MatchString(fb.Kind) {
		return fmt.Errorf("feedback needs node_id and a kind such as wrong, outdated or missing")
	}
	if err := limit("comment", fb.Comment, 0, maxReason); err != nil {
		return err
	}
	if accessFrom(ctx) == nil {
		c := CallerFrom(ctx)
		if c == nil {
			c = fb.Caller // rest.go sets it from the request context, not from the body
		}
		ctx = withAccess(ctx, f.s.restAccess(c))
	}
	if err := f.s.canWrite(ctx); err != nil {
		return err
	}
	if err := f.s.visibleNodes(ctx, fb.NodeID); err != nil {
		return err
	}
	a := accessFrom(ctx)
	_, _, _, err := f.s.backend().CreateProposal(ctx, proposalProps(map[string]any{"kind": "feedback",
		"feedback_kind": fb.Kind, "target_id": fb.NodeID, "text": fb.Comment}, a.User, a.Via, "rest"))
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
	a := s.accessFor(user, c.Groups, nil)
	a.Via = "rest"
	return a
}

// Caller is an IdentityFunc for the REST layer: it checks the bearer token like /mcp does.
func (o *OIDC) Caller(r *http.Request) (*Caller, error) {
	fields := strings.Fields(r.Header.Get("Authorization"))
	if len(fields) != 2 || !strings.EqualFold(fields[0], "bearer") {
		return nil, fmt.Errorf("no bearer token")
	}
	info, err := o.Verify(r.Context(), fields[1], r)
	if err != nil {
		return nil, err
	}
	name, _ := info.Extra["user"].(string)
	groups, _ := info.Extra["groups"].([]string)
	return &Caller{Subject: info.UserID, Name: name, Groups: groups}, nil
}
