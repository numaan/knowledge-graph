package main

import (
	"context"
	"fmt"
	"path"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Summaries written by kg_pipeline/summaries.py: (:Summary {level, text, target_id})-[:SUMMARIZES]->(target),
// with level file (target File), module (Folder), repo (Repository) or domain (Capability).

type SummaryIn struct {
	NodeID string `json:"node_id,omitempty" jsonschema:"id of a file, symbol, chunk, folder, capability, concept, rule or summary"`
	Path   string `json:"path,omitempty" jsonschema:"file or folder path inside the repository, e.g. backend/app/routers"`
	Repo   string `json:"repo,omitempty" jsonschema:"repository name; alone it returns the repository summary"`
	Domain string `json:"domain,omitempty" jsonschema:"capability name, capability id or module folder"`
	Page
}

func folderID(repo, dir string) string {
	if dir == "" || dir == "." {
		dir = "/"
	}
	return repo + ":folder:" + dir
}

// folderTargets lists a folder, each parent folder and the repository: the summary targets above a file.
func folderTargets(repo, dir string) []string {
	dir = strings.Trim(dir, "/")
	out := []string{}
	for {
		out = append(out, folderID(repo, dir))
		if dir == "" {
			break
		}
		if dir = path.Dir(dir); dir == "." {
			dir = ""
		}
	}
	return append(out, repo)
}

// summaryTargets lists the summary targets for a path, nearest first: the file, the folder of that
// name, its parent folders, then the repository. For a folder path the file step finds nothing.
func summaryTargets(repo, p string) []string {
	if p = strings.Trim(strings.ReplaceAll(p, "\\", "/"), "/"); p == "" {
		return folderTargets(repo, "")
	}
	return append([]string{repo + ":" + p}, folderTargets(repo, p)...)
}

// stale fields are set only on outdated nodes; drop the null ones so results stay short.
func dropEmptyStale(rows []map[string]any) []map[string]any {
	for _, r := range rows {
		for _, k := range []string{"stale", "stale_reason"} {
			if v, ok := r[k]; ok && (v == nil || v == false) {
				delete(r, k)
			}
		}
	}
	return rows
}

// targets resolves the input to (repo, ordered summary targets, requested level).
func (s *Server) summaryRequest(ctx context.Context, in SummaryIn) (string, []string, string, error) {
	repo := s.repo(in.Repo)
	switch {
	case in.NodeID != "":
		n := one(must(s.graph.Read(ctx, `
			CALL () { MATCH (n:Code {id: $id}) RETURN n UNION MATCH (n:Domain {id: $id}) RETURN n
			          UNION MATCH (n:Summary {id: $id}) RETURN n UNION MATCH (n:Folder {id: $id}) RETURN n }
			WITH n WHERE $repos IS NULL OR n.repo IS NULL OR n.repo IN $repos
			RETURN labels(n) AS labels, n.repo AS repo, n.path AS path, n.module AS module,
			       coalesce(n.modules, [])[0] AS concept_module, n.target_id AS target, n.level AS level`,
			map[string]any{"id": in.NodeID})))
		if n == nil {
			return "", nil, "", fmt.Errorf("node %s not found; use search_graph or find_symbol for an id", in.NodeID)
		}
		repo, _ = n["repo"].(string)
		labels := map[string]bool{}
		for _, l := range n["labels"].([]any) {
			labels[l.(string)] = true
		}
		p, _ := n["path"].(string)
		switch {
		case labels["Summary"]:
			return repo, []string{n["target"].(string)}, fmt.Sprint(n["level"]), nil
		case labels["Repository"]:
			return in.NodeID, []string{in.NodeID}, "repo", nil
		case labels["Folder"]:
			return repo, folderTargets(repo, p), "module", nil
		case labels["Capability"]:
			m, _ := n["module"].(string)
			return repo, append([]string{in.NodeID}, folderTargets(repo, m)...), "domain", nil
		case labels["Domain"]:
			m, _ := n["module"].(string)
			if m == "" {
				m, _ = n["concept_module"].(string)
			}
			capability := m
			if capability == "" {
				capability = "/" // kg-domain names the root module "/"
			}
			return repo, append([]string{repo + ":capability:" + capability}, folderTargets(repo, m)...), "domain", nil
		case p != "":
			return repo, summaryTargets(repo, p), "file", nil
		}
		return "", nil, "", fmt.Errorf("node %s has no summary level (summaries cover code and domain nodes)", in.NodeID)
	case in.Domain != "":
		c := one(must(s.graph.Read(ctx, `MATCH (c:Capability)
			WHERE (c.id = $d OR toLower(c.name) = toLower($d) OR c.module = $d) AND ($repo = '' OR c.repo = $repo)
			  AND ($repos IS NULL OR c.repo IN $repos)
			RETURN c.id AS id, c.repo AS repo, c.module AS module ORDER BY c.id = $d DESC, c.repo LIMIT 1`,
			map[string]any{"d": strings.TrimSpace(in.Domain), "repo": repo})))
		if c == nil {
			if repo == "" {
				return "", nil, "", fmt.Errorf("no capability %q; call domain_overview to list them", in.Domain)
			}
			return repo, []string{repo}, "domain", nil
		}
		repo = c["repo"].(string)
		m, _ := c["module"].(string)
		return repo, append([]string{c["id"].(string)}, folderTargets(repo, m)...), "domain", nil
	case repo == "":
		return "", nil, "", fmt.Errorf("give node_id, path, domain or repo (no default repository is set)")
	case in.Path != "":
		return repo, summaryTargets(repo, in.Path), "file", nil
	}
	return repo, []string{repo}, "repo", nil
}

func (s *Server) registerSummaries(m *mcp.Server) {
	addTool(m, &mcp.Tool{Name: "get_summary", Description: "Short natural-language summary of a file, folder " +
		"(module), repository or business capability, with the summaries one level down. Give node_id, path, " +
		"domain or repo. When that level has no summary, it returns the nearest one above it (file, then folder, " +
		"parent folders, repository) and says so. Use it to orient yourself before you read code."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in SummaryIn) (*mcp.CallToolResult, Doc, error) {
			repo, targets, level, err := s.summaryRequest(ctx, in)
			if err != nil {
				return nil, Doc{}, err
			}
			if !s.allowedRepo(ctx, repo) {
				return nil, Doc{}, fmt.Errorf("repository %s is not visible to this server", repo)
			}
			sum := one(must(s.graph.Read(ctx, `UNWIND range(0, size($targets) - 1) AS i
				MATCH (s:Summary {target_id: $targets[i]}) WHERE $repos IS NULL OR s.repo IN $repos
				RETURN s.id AS id, s.level AS level, s.text AS text, s.model AS model, toString(s.generated_at) AS generated_at,
				       s.target_id AS target_id, s.path AS path, s.repo AS repo, s.stale AS stale, s.stale_reason AS stale_reason, i
				ORDER BY i LIMIT 1`, map[string]any{"targets": targets})))
			if sum == nil {
				return nil, Doc{}, fmt.Errorf("no summary for %s or the levels above it; run kg-summaries --repo %s", targets[0], repo)
			}
			data := map[string]any{"requested_level": level, "summary": sum}
			if i, _ := sum["i"].(int64); i > 0 {
				data["note"] = fmt.Sprintf("no %s summary for %s; this is the nearest %s level", level, targets[0], sum["level"])
			}
			delete(sum, "i")
			dropEmptyStale([]map[string]any{sum})
			p := map[string]any{"repo": sum["repo"], "path": sum["path"], "target": sum["target_id"]}
			var children []map[string]any
			switch sum["level"] {
			case "module":
				children = must(s.graph.Read(ctx, `MATCH (s:Summary {repo: $repo, level: 'file'})
					WHERE s.path STARTS WITH CASE $path WHEN '' THEN '' ELSE $path + '/' END
					  AND NOT substring(s.path, CASE $path WHEN '' THEN 0 ELSE size($path) + 1 END) CONTAINS '/'
					RETURN s.target_id AS id, s.path AS path, left(s.text, 400) AS text ORDER BY path LIMIT 50`, p))
			case "repo":
				children = must(s.graph.Read(ctx, `MATCH (s:Summary {repo: $repo, level: 'module'})
					OPTIONAL MATCH (:Folder {id: s.target_id})-[:CONTAINS]->(f:File)
					WITH s, count(f) AS files
					RETURN s.target_id AS id, s.path AS path, files, left(s.text, 400) AS text ORDER BY files DESC, path LIMIT 50`, p))
			case "domain":
				children = must(s.graph.Read(ctx, `MATCH (:Capability {id: $target})-[:USES_CONCEPT]->(c:DomainConcept)
					RETURN c.id AS id, c.name AS name, c.kind AS kind, c.definition AS definition ORDER BY name LIMIT 50`, p))
			}
			if children != nil {
				data["children"] = children
			}
			return nil, Doc{Data: data}, nil
		})
}
