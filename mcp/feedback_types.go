package main

import (
	"context"
	"time"
)

// Copies of the REST types of stream 6 (rest.go on branch claude/kb-web-e20d1d), so that this
// branch builds alone. Delete this file when rest.go is merged; the definitions are identical.

type Caller struct {
	Subject string   `json:"subject"`
	Name    string   `json:"name,omitempty"`
	Email   string   `json:"email,omitempty"`
	Groups  []string `json:"groups,omitempty"`
}

type callerKey struct{}

func WithCaller(ctx context.Context, c *Caller) context.Context {
	return context.WithValue(ctx, callerKey{}, c)
}

func CallerFrom(ctx context.Context) *Caller {
	c, _ := ctx.Value(callerKey{}).(*Caller)
	return c
}

type Feedback struct {
	NodeID  string    `json:"node_id"`
	Kind    string    `json:"kind"` // wrong, outdated, helpful, missing
	Comment string    `json:"comment,omitempty"`
	Caller  *Caller   `json:"caller,omitempty"`
	At      time.Time `json:"at"`
}

type FeedbackSink interface {
	Submit(ctx context.Context, f Feedback) error
}
