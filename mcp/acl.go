package main

import (
	"strings"
)

// Access control. Every :Source node carries acl, a list of groups such as "jira:PREC",
// "confluence:ENG" or "repo:precept". The server sees a node when one of its groups is in
// KG_ACL_GROUPS ("*" sees everything). Nodes without acl are public.
//
// Deploy one server per audience (team, agent) with its own KG_ACL_GROUPS; the agent cannot
// change its groups, because they come from the environment, not from a tool argument.
func aclGroups() map[string]bool {
	out := map[string]bool{}
	for _, g := range strings.Split(env("KG_ACL_GROUPS", "*"), ",") {
		if g = strings.TrimSpace(g); g != "" {
			out[g] = true
		}
	}
	return out
}

func (s *Server) allowed(acl any) bool {
	if s.groups["*"] {
		return true
	}
	list, ok := acl.([]any)
	if !ok || len(list) == 0 {
		return true
	}
	for _, g := range list {
		if name, _ := g.(string); s.groups[name] {
			return true
		}
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
func (s *Server) allowedRepo(repo string) bool {
	return s.groups["*"] || repo == "" || s.groups["repo:"+repo]
}
