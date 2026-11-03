package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"hash/fnv"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Result size limits, so one tool call cannot overflow the context of an agent.
//
// Every tool input embeds Page. List results (Rows) are cut to whole rows that fit in max_chars
// and get an opaque next_cursor for the rest. Document results (Doc) keep their shape: long
// strings are cut with a marker and long lists are shortened, and Doc.omitted counts what was
// left out. Tools compute the full result and the page is cut from it, so a cursor works as long
// as the graph does not change between the calls.

const (
	defaultMaxChars = 30000
	minMaxChars     = 1000
	maxMaxChars     = 200000
	envelopeChars   = 300 // note, cursor and field names around the rows
)

type Page struct {
	MaxChars int    `json:"max_chars,omitempty" jsonschema:"size budget of the result in characters, 1000-200000 (default 30000); longer results are cut and marked"`
	Cursor   string `json:"cursor,omitempty" jsonschema:"next_cursor of the previous call with the same arguments, to get the next page"`
}

func (p Page) page() Page { return p }

type pageable interface{ page() Page }

// addTool registers a tool like mcp.AddTool and applies the Page limits to its output.
func addTool[In pageable, Out any](m *mcp.Server, t *mcp.Tool, h mcp.ToolHandlerFor[In, Out]) {
	mcp.AddTool(m, t, func(ctx context.Context, req *mcp.CallToolRequest, in In) (*mcp.CallToolResult, Out, error) {
		var zero Out
		p := in.page()
		key := argsKey(in)
		offset := 0
		if p.Cursor != "" {
			var err error
			if offset, err = decodeCursor(p.Cursor, key); err != nil {
				return nil, zero, err
			}
		}
		res, out, err := h(ctx, req, in)
		if err != nil {
			return res, out, err
		}
		budget := clamp(p.MaxChars, minMaxChars, maxMaxChars, defaultMaxChars)
		switch o := any(&out).(type) {
		case *Rows:
			*o = pageRows(*o, offset, budget, key)
		case *Doc:
			if p.Cursor != "" {
				return nil, zero, fmt.Errorf("%s returns one document; cursor applies to list results only", t.Name)
			}
			*o = fitDoc(*o, budget)
		}
		return res, out, nil
	})
}

// argsKey ties a cursor to the arguments it was made for (without cursor and max_chars).
func argsKey(in any) string {
	b, _ := json.Marshal(in)
	var m map[string]any
	json.Unmarshal(b, &m)
	delete(m, "cursor")
	delete(m, "max_chars")
	b, _ = json.Marshal(m) // map keys are sorted
	h := fnv.New32a()
	h.Write(b)
	return strconv.FormatUint(uint64(h.Sum32()), 36)
}

func encodeCursor(key string, offset int) string {
	return base64.RawURLEncoding.EncodeToString([]byte("v1:" + key + ":" + strconv.Itoa(offset)))
}

func decodeCursor(c, key string) (int, error) {
	b, err := base64.RawURLEncoding.DecodeString(c)
	parts := strings.Split(string(b), ":")
	if err != nil || len(parts) != 3 || parts[0] != "v1" {
		return 0, fmt.Errorf("invalid cursor; use the next_cursor value of the previous result")
	}
	if parts[1] != key {
		return 0, fmt.Errorf("cursor belongs to a call with other arguments; repeat the same arguments with the cursor")
	}
	offset, err := strconv.Atoi(parts[2])
	if err != nil || offset < 0 {
		return 0, fmt.Errorf("invalid cursor offset")
	}
	return offset, nil
}

func jsonSize(v any) int {
	b, _ := json.Marshal(v)
	return len(b)
}

// pageRows keeps the whole rows from offset that fit in budget. A first row that is larger than
// the budget alone is shrunk, so every page makes progress.
func pageRows(r Rows, offset, budget int, key string) Rows {
	all := r.Results
	offset = min(offset, len(all))
	used := envelopeChars + len(r.Note)
	out := []map[string]any{}
	shrunk := false
	for _, row := range all[offset:] {
		n := jsonSize(row) + 1
		if used+n > budget {
			if len(out) > 0 {
				break
			}
			small, _ := shrink(row, budget-used)
			row, n, shrunk = small.(map[string]any), jsonSize(small)+1, true
		}
		out = append(out, row)
		used += n
	}
	r.Results = out
	end := offset + len(out)
	if end < len(all) {
		r.NextCursor = encodeCursor(key, end)
	}
	if offset > 0 || end < len(all) || shrunk {
		r.Truncated = true
		r.Total = len(all)
		page := fmt.Sprintf("rows %d-%d of %d", offset+1, end, len(all))
		if end < len(all) {
			page += "; call again with cursor=next_cursor for more, or raise max_chars"
		}
		if shrunk {
			page += "; long fields cut to fit max_chars"
		}
		r.Note = strings.TrimPrefix(r.Note+"; "+page, "; ")
	}
	return r
}

func fitDoc(d Doc, budget int) Doc {
	small, omitted := shrink(d.Data, budget-envelopeChars)
	if omitted == nil {
		return d
	}
	d.Data, _ = small.(map[string]any)
	d.Truncated = true
	if len(omitted) > 0 {
		d.Omitted = omitted
	}
	return d
}

// Shrink steps: maximum string length and maximum list length, from mild to hard.
var shrinkSteps = []struct{ str, list int }{{4000, 100}, {2000, 50}, {1000, 25}, {500, 12}, {250, 6}, {120, 3}, {60, 1}}

// shrink returns v unchanged with nil omitted when it fits. Otherwise it returns a copy cut at
// the mildest step that fits (or the hardest step) and the number of list items left out per
// path. Cut strings end with a "…[cut N chars]" marker.
func shrink(v any, budget int) (any, map[string]int) {
	if jsonSize(v) <= budget {
		return v, nil
	}
	var out any
	var omitted map[string]int
	for _, s := range shrinkSteps {
		omitted = map[string]int{}
		out = cut(v, s.str, s.list, "", omitted)
		if jsonSize(out) <= budget {
			break
		}
	}
	return out, omitted
}

func cut(v any, maxStr, maxList int, path string, omitted map[string]int) any {
	switch x := v.(type) {
	case string:
		return cutString(x, maxStr)
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			out[k] = cut(e, maxStr, maxList, joinPath(path, k), omitted)
		}
		return out
	case []any:
		keep := min(len(x), maxList)
		out := make([]any, keep)
		for i := range keep {
			out[i] = cut(x[i], maxStr, maxList, path, omitted)
		}
		if len(x) > keep {
			omitted[path] += len(x) - keep
		}
		return out
	case []map[string]any:
		keep := min(len(x), maxList)
		out := make([]map[string]any, keep)
		for i := range keep {
			out[i] = cut(x[i], maxStr, maxList, path, omitted).(map[string]any)
		}
		if len(x) > keep {
			omitted[path] += len(x) - keep
		}
		return out
	case []string:
		keep := min(len(x), maxList)
		out := make([]string, keep)
		for i := range keep {
			out[i] = cutString(x[i], maxStr)
		}
		if len(x) > keep {
			omitted[path] += len(x) - keep
		}
		return out
	}
	return v
}

func joinPath(path, key string) string {
	if path == "" {
		return key
	}
	return path + "." + key
}

func cutString(s string, n int) string {
	if len(s) <= n {
		return s
	}
	head := s[:n]
	for len(head) > 0 && !utf8.ValidString(head) {
		head = head[:len(head)-1]
	}
	return head + fmt.Sprintf("…[cut %d chars]", len(s)-len(head))
}
