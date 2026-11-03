package main

import (
	"context"
	"strings"
)

// Access control. Every :Source node carries acl, a list of groups such as "jira:PREC",
// "confluence:ENG", "jira-group:precept-devs" or "repo:precept". A caller sees a node when one of
// its groups is in the acl ("*" sees everything). Nodes without acl are public.
//
// The caller's groups come from the request context (access.go): from the OIDC token in HTTP mode,
// from KG_ACL_GROUPS otherwise. KG_ACL_GROUPS is also the upper bound for every token. The agent
// cannot change its groups, because they never come from a tool argument.
func aclGroups() map[string]bool {
	out := map[string]bool{}
	for _, g := range strings.Split(env("KG_ACL_GROUPS", "*"), ",") {
		if g = strings.TrimSpace(g); g != "" {
			out[g] = true
		}
	}
	return out
}

func (s *Server) allowed(ctx context.Context, acl any) bool {
	groups := s.groupsFor(ctx)
	if groups["*"] {
		return true
	}
	list, ok := acl.([]any)
	if !ok || len(list) == 0 {
		return true
	}
	for _, g := range list {
		if name, _ := g.(string); groups[name] {
			return true
		}
	}
	if a := accessFrom(ctx); a != nil && a.stats != nil {
		a.stats.hidden.Add(1)
	}
	return false
}

// repoScope is the $repos query parameter: nil (all repositories) or the repo:<name> groups.
func repoScope(groups map[string]bool) any {
	if groups["*"] {
		return nil
	}
	repos := []string{}
	for g := range groups {
		if name, ok := strings.CutPrefix(g, "repo:"); ok {
			repos = append(repos, name)
		}
	}
	return repos
}

// allowedRepo checks the repo:<name> group for code results.
func (s *Server) allowedRepo(ctx context.Context, repo string) bool {
	groups := s.groupsFor(ctx)
	return groups["*"] || repo == "" || groups["repo:"+repo]
}
