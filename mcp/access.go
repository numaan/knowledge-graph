package main

import (
	"context"
	"fmt"
	"os"
	"slices"
	"strings"
	"sync/atomic"

	"gopkg.in/yaml.v3"
)

// Per-request access. Each tool call carries an Access in its context: the caller and the ACL
// groups the caller may see. HTTP with OIDC takes the groups from the bearer token (auth.go) and
// maps them through acl-mapping.yaml; stdio and HTTP without OIDC use KG_ACL_GROUPS. The groups
// of the server (KG_ACL_GROUPS) are the default and the upper bound: a token never sees more.
//
// The REST layer calls the same handlers: it puts s.accessFor(user, tokenGroups, scopes) into the
// context with withAccess before it calls them.

// gov holds the governance settings of the Server (embedded, so tools.go stays small).
type gov struct {
	mapping       *aclMapping     // token groups -> ACL groups
	requireCaller bool            // OIDC mode: no Access in the context means no protected data
	writeScope    string          // OAuth scope needed for the write-back tools; empty: none
	audit         *auditLog       // one JSON line per tool call
	proposals     proposalBackend // node lookup and :Proposal writes; nil uses the graph
}

type Access struct {
	User   string
	Groups map[string]bool // effective groups, already bounded by the server groups
	Scopes []string        // OAuth scopes of the token (empty in stdio mode)
	Via    string          // "token" or "env"
	stats  *callStats
}

// callStats is filled during one tool call and written to the audit log.
type callStats struct {
	denied atomic.Bool
	hidden atomic.Int64 // results removed by the ACL filter
}

type accessKey struct{}

func withAccess(ctx context.Context, a *Access) context.Context {
	if a.stats == nil {
		a.stats = &callStats{}
	}
	return context.WithValue(ctx, accessKey{}, a)
}

func accessFrom(ctx context.Context) *Access {
	a, _ := ctx.Value(accessKey{}).(*Access)
	return a
}

// groupsFor returns the caller's groups. Without an Access in the context it falls back to the
// server groups, except in OIDC mode, where a call without a caller sees no protected data.
func (s *Server) groupsFor(ctx context.Context) map[string]bool {
	if a := accessFrom(ctx); a != nil {
		return a.Groups
	}
	if s.requireCaller {
		return map[string]bool{}
	}
	return s.groups
}

// setupGovernance reads the ACL mapping and the audit settings. stdio: KG_ACL_GROUPS for the one
// local caller. HTTP with OIDC: groups from each token, bounded by KG_ACL_GROUPS.
func (s *Server) setupGovernance(stdio bool, oidc *OIDC) error {
	m, err := loadACLMapping(os.Getenv("KG_ACL_MAPPING"))
	if err != nil {
		return err
	}
	s.mapping, s.writeScope = m, os.Getenv("KG_OIDC_WRITE_SCOPE")
	s.requireCaller = oidc != nil && !stdio
	s.audit, err = newAuditLog(stdio)
	return err
}

func (s *Server) envAccess() *Access {
	return &Access{User: env("KG_USER", "local"), Groups: s.groups, Via: "env"}
}

// accessFor maps token groups to ACL groups and bounds them by the server groups.
func (s *Server) accessFor(user string, tokenGroups, scopes []string) *Access {
	return &Access{User: user, Groups: bound(s.mapping.resolve(tokenGroups), s.groups), Scopes: scopes, Via: "token"}
}

// bound limits the requested groups to the upper bound. "*" in the bound allows everything;
// "*" in the request (an admin token) gets exactly the bound.
func bound(requested, upper map[string]bool) map[string]bool {
	out := map[string]bool{}
	if upper["*"] {
		for g := range requested {
			out[g] = true
		}
		return out
	}
	for g := range requested {
		if upper[g] {
			out[g] = true
		}
	}
	if requested["*"] {
		for g := range upper {
			out[g] = true
		}
	}
	return out
}

// deny marks the call as denied in the audit log and returns the error for the agent.
func deny(ctx context.Context, format string, args ...any) error {
	if a := accessFrom(ctx); a != nil && a.stats != nil {
		a.stats.denied.Store(true)
	}
	return fmt.Errorf(format, args...)
}

func sortedGroups(groups map[string]bool) []string {
	out := make([]string, 0, len(groups))
	for g := range groups {
		out = append(out, g)
	}
	slices.Sort(out)
	return out
}

// ---- acl-mapping.yaml ----------------------------------------------------------------------------

// aclMapping maps identity-provider groups (the token's groups claim) to ACL groups:
//
//	default: []                       # groups every authenticated caller gets
//	passthrough: true                 # keep token groups that already are ACL groups (repo:x, jira:X, ...)
//	groups:
//	  kg-admins: ["*"]
//	  team-precept: [repo:precept, jira:PREC, confluence:ENG]
//	  precept-devs: [jira-group:precept-devs]
//
// Keycloak sends group paths ("/team-precept"); the leading slash is ignored.
type aclMapping struct {
	Default     []string            `yaml:"default"`
	Passthrough *bool               `yaml:"passthrough"`
	Groups      map[string][]string `yaml:"groups"`
}

var aclPrefixes = []string{"repo:", "jira:", "jira-group:", "confluence:", "confluence-group:", "docs:"}

func loadACLMapping(path string) (*aclMapping, error) {
	m := &aclMapping{}
	if path == "" {
		return m, nil
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("acl mapping: %w", err)
	}
	if err := yaml.Unmarshal(b, m); err != nil {
		return nil, fmt.Errorf("acl mapping %s: %w", path, err)
	}
	norm := map[string][]string{}
	for k, v := range m.Groups {
		norm[normGroup(k)] = v
	}
	m.Groups = norm
	return m, nil
}

func normGroup(g string) string {
	return strings.ToLower(strings.TrimPrefix(strings.TrimSpace(g), "/"))
}

func (m *aclMapping) resolve(tokenGroups []string) map[string]bool {
	out := map[string]bool{}
	if m == nil {
		m = &aclMapping{}
	}
	for _, g := range m.Default {
		out[g] = true
	}
	for _, raw := range tokenGroups {
		g := normGroup(raw)
		for _, mapped := range m.Groups[g] {
			out[mapped] = true
		}
		if m.Passthrough == nil || *m.Passthrough {
			for _, p := range aclPrefixes {
				if strings.HasPrefix(g, p) {
					v := strings.TrimPrefix(strings.TrimSpace(raw), "/") // keep the case of jira:PREC
					if strings.HasSuffix(p, "-group:") {
						v = g // Atlassian group names are case-insensitive; the pipeline writes them in lower case
					}
					out[v] = true
					break
				}
			}
		}
	}
	return out
}
