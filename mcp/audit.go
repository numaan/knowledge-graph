package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"slices"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Audit log: one JSON line per tool call. It records who called which tool, with a hash and a
// short summary of the arguments, the number of results, the latency and whether the ACL denied
// the call or hid results. Free text (queries, Cypher, reasons) is logged as its length only, and
// the log never holds tokens or results.
//
// KG_AUDIT: stdout (default for HTTP), stderr (default for stdio, where stdout is the protocol)
// or off. KG_AUDIT_FILE: also append the lines to this file.

type auditRecord struct {
	Time      string   `json:"time"`
	Event     string   `json:"event"`
	User      string   `json:"user"`
	Via       string   `json:"via"`
	Groups    []string `json:"groups"`
	Tool      string   `json:"tool"`
	ArgsHash  string   `json:"args_sha256"`
	Summary   string   `json:"args_summary"`
	Results   int      `json:"results"`
	Hidden    int64    `json:"hidden"`
	LatencyMS int64    `json:"latency_ms"`
	Denied    bool     `json:"denied"`
	Error     bool     `json:"error"`
}

type auditLog struct {
	mu  sync.Mutex
	out []io.Writer
}

func newAuditLog(stdio bool) (*auditLog, error) {
	a := &auditLog{}
	def := "stdout"
	if stdio {
		def = "stderr"
	}
	switch mode := env("KG_AUDIT", def); mode {
	case "stdout":
		if stdio {
			return nil, fmt.Errorf("KG_AUDIT=stdout is not possible in stdio mode (stdout is the MCP protocol)")
		}
		a.out = append(a.out, os.Stdout)
	case "stderr":
		a.out = append(a.out, os.Stderr)
	case "off":
	default:
		return nil, fmt.Errorf("KG_AUDIT must be stdout, stderr or off, not %q", mode)
	}
	if path := os.Getenv("KG_AUDIT_FILE"); path != "" {
		f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
		if err != nil {
			return nil, fmt.Errorf("audit file: %w", err)
		}
		a.out = append(a.out, f)
	}
	return a, nil
}

func (a *auditLog) write(r auditRecord) {
	if a == nil || len(a.out) == 0 {
		return
	}
	b, _ := json.Marshal(r)
	b = append(b, '\n')
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, w := range a.out {
		if _, err := w.Write(b); err != nil {
			log.Printf("audit: %v", err)
		}
	}
}

// accessMiddleware gives every tool call its Access (from the token or the environment) and
// writes the audit line. Calls without a token in OIDC mode cannot happen (the HTTP layer answers
// 401), but are refused here too.
func (s *Server) accessMiddleware(next mcp.MethodHandler) mcp.MethodHandler {
	return func(ctx context.Context, method string, req mcp.Request) (mcp.Result, error) {
		call, ok := req.(*mcp.CallToolRequest)
		if method != "tools/call" || !ok {
			return next(ctx, method, req)
		}
		a := s.envAccess()
		if extra := req.GetExtra(); extra != nil && extra.TokenInfo != nil {
			user, _ := extra.TokenInfo.Extra["user"].(string)
			groups, _ := extra.TokenInfo.Extra["groups"].([]string)
			a = s.accessFor(user, groups, extra.TokenInfo.Scopes)
		} else if c := CallerFrom(ctx); c != nil { // the REST layer (rest.go) set the caller
			a = s.restAccess(c)
		} else if s.requireCaller {
			a = &Access{User: "anonymous", Groups: map[string]bool{}, Via: "none"}
		}
		ctx = withAccess(ctx, a)
		start := time.Now()
		var res mcp.Result
		var err error
		if a.Via == "none" {
			err = deny(ctx, "authentication required")
		} else {
			res, err = next(ctx, method, req)
		}
		rec := auditRecord{Time: start.UTC().Format(time.RFC3339Nano), Event: "tool_call", User: a.User, Via: a.Via,
			Groups: sortedGroups(a.Groups), Tool: call.Params.Name, ArgsHash: argsHash(call.Params.Arguments),
			Summary: argsSummary(call.Params.Arguments), LatencyMS: time.Since(start).Milliseconds(),
			Denied: a.stats.denied.Load(), Hidden: a.stats.hidden.Load(), Error: err != nil}
		if r, ok := res.(*mcp.CallToolResult); ok && r != nil {
			rec.Error = rec.Error || r.IsError
			rec.Results = resultCount(r.StructuredContent)
		}
		s.audit.write(rec)
		return res, err
	}
}

func rawArgs(args any) []byte {
	if raw, ok := args.(json.RawMessage); ok {
		return raw
	}
	b, _ := json.Marshal(args)
	return b
}

func argsHash(args any) string {
	sum := sha256.Sum256(rawArgs(args))
	return hex.EncodeToString(sum[:8])
}

// freeText are arguments that can hold prose or code; the audit log keeps only their length.
var freeText = map[string]bool{"query": true, "text": true, "reason": true, "comment": true, "title": true,
	"requirement": true, "params": true}

func argsSummary(args any) string {
	var m map[string]any
	if json.Unmarshal(rawArgs(args), &m) != nil {
		return ""
	}
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		var v string
		switch x := m[k].(type) {
		case string:
			if freeText[k] {
				v = fmt.Sprintf("<%d chars>", utf8.RuneCountInString(x))
			} else {
				v = truncate(x, 64)
			}
		case []any:
			v = fmt.Sprintf("[%d]", len(x))
		case map[string]any:
			v = fmt.Sprintf("{%d}", len(x))
		default:
			v = fmt.Sprint(x)
		}
		parts = append(parts, k+"="+v)
	}
	return truncate(strings.Join(parts, " "), 300)
}

func truncate(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n]) + "..."
}

// resultCount is the length of "results" (Rows), 1 for a non-empty "data" (Doc) or another
// non-empty object (a proposal), else 0.
func resultCount(structured any) int {
	var out map[string]json.RawMessage
	b, err := json.Marshal(structured)
	if err != nil || json.Unmarshal(b, &out) != nil || len(out) == 0 {
		return 0
	}
	if raw, ok := out["results"]; ok {
		var list []json.RawMessage
		json.Unmarshal(raw, &list)
		return len(list)
	}
	if raw, ok := out["data"]; ok && (string(raw) == "null" || string(raw) == "{}") {
		return 0
	}
	return 1
}
