package main

import (
	"fmt"
	"regexp"
	"strings"
)

// The read transaction already blocks writes; this guard gives agents a clear error first and
// blocks procedures that read outside the graph or change server state.
var (
	writeClause = regexp.MustCompile(`(?i)\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|LOAD\s+CSV|ALTER|GRANT|DENY|REVOKE|START\s+DATABASE|STOP\s+DATABASE)\b`)
	callProc    = regexp.MustCompile(`(?i)\bCALL\s+([a-zA-Z0-9_.]+)`)
	comments    = regexp.MustCompile(`(?s)//[^\n]*|/\*.*?\*/`)
	stringLits  = regexp.MustCompile(`'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"`)
	edgeType    = regexp.MustCompile(`^[A-Z][A-Z0-9_]*$`)
)

var allowedProcedures = []string{"db.index.fulltext.querynodes", "db.index.vector.querynodes", "db.labels",
	"db.relationshiptypes", "db.propertykeys", "db.schema.visualization"}

func checkReadOnly(cypher string) error {
	code := stringLits.ReplaceAllString(comments.ReplaceAllString(cypher, " "), "''")
	if m := writeClause.FindString(code); m != "" {
		return fmt.Errorf("read-only server: %q is not allowed", strings.ToUpper(m))
	}
	for _, m := range callProc.FindAllStringSubmatch(code, -1) {
		name := strings.ToLower(m[1])
		ok := false
		for _, a := range allowedProcedures {
			if name == a {
				ok = true
				break
			}
		}
		if !ok {
			return fmt.Errorf("procedure %q is not allowed; allowed: %s", m[1], strings.Join(allowedProcedures, ", "))
		}
	}
	return nil
}

func checkEdgeTypes(types []string) error {
	for _, t := range types {
		if !edgeType.MatchString(t) {
			return fmt.Errorf("invalid edge type %q (use names such as CALLS, IMPORTS, REALISES)", t)
		}
	}
	return nil
}
