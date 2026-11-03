package main

import (
	"context"
	"fmt"
	"regexp"
)

// Feedback from the REST API (POST /api/feedback). If the REST layer (rest.go) defines Feedback
// and FeedbackSink, keep its definitions and delete them here; graphFeedback stays.

type Feedback struct {
	NodeID  string `json:"node_id"`
	Kind    string `json:"kind"` // e.g. wrong, outdated, missing, helpful
	Comment string `json:"comment"`
	User    string `json:"user"`
}

type FeedbackSink interface {
	Submit(ctx context.Context, f Feedback) error
}

// graphFeedback stores feedback as a :Proposal (kind "feedback") for kg-review. The caller must
// see the node. The user comes from the Access in the context when the caller has a token, so a
// client cannot write feedback in another user's name.
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
	if err := f.s.canWrite(ctx); err != nil {
		return err
	}
	if err := f.s.visibleNodes(ctx, fb.NodeID); err != nil {
		return err
	}
	a := accessFrom(ctx)
	if a == nil || a.Via != "token" {
		user := fb.User
		if user == "" {
			user = "anonymous"
		}
		groups := f.s.groupsFor(ctx)
		a = &Access{User: user, Groups: groups, Via: "rest"}
	}
	_, _, _, err := f.s.backend().CreateProposal(ctx, proposalProps(map[string]any{"kind": "feedback",
		"feedback_kind": fb.Kind, "target_id": fb.NodeID, "text": fb.Comment}, a.User, a.Via, "rest"))
	return err
}
