// kg-load: load test for the kg MCP server over Streamable HTTP.
//
//	go run ./cmd/kg-load -url http://localhost:8000/mcp -clients 1,10,50 -duration 30s -out load.json
//
// Each client opens its own MCP session and calls the tools in turn, with arguments taken from the
// graph (find_symbol with -symbol-prefix, search_knowledge for ticket keys). It prints p50, p95 and
// p99 latency and the error count per tool for each concurrency level.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math/rand/v2"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type Stat struct {
	Tool    string  `json:"tool"`
	Clients int     `json:"clients"`
	Calls   int     `json:"calls"`
	Errors  int     `json:"errors"`
	P50ms   float64 `json:"p50_ms"`
	P95ms   float64 `json:"p95_ms"`
	P99ms   float64 `json:"p99_ms"`
	MeanMs  float64 `json:"mean_ms"`
	PerSec  float64 `json:"calls_per_second"`
}

// workload holds the arguments the tools are called with.
type workload struct {
	repo    string
	queries []string
	symbols []map[string]any // id and name
	tickets []string
}

func (w *workload) args(tool string, r *rand.Rand) map[string]any {
	q := w.queries[r.IntN(len(w.queries))]
	var sym map[string]any
	if len(w.symbols) > 0 {
		sym = w.symbols[r.IntN(len(w.symbols))]
	}
	switch tool {
	case "search_graph":
		return map[string]any{"query": q, "repo": w.repo, "k": 8}
	case "search_knowledge":
		return map[string]any{"query": q, "k": 8}
	case "find_symbol":
		return map[string]any{"name": sym["name"], "repo": w.repo}
	case "get_symbol_code", "find_related_tests", "get_change_history":
		return map[string]any{"symbol": sym["id"]}
	case "impact_analysis":
		return map[string]any{"symbol": sym["id"], "depth": 2}
	case "get_neighbors":
		return map[string]any{"node_id": sym["id"], "limit": 50}
	case "get_context_for_ticket":
		if len(w.tickets) == 0 {
			return map[string]any{"key": "NONE-1"}
		}
		return map[string]any{"key": w.tickets[r.IntN(len(w.tickets))]}
	case "domain_overview":
		return map[string]any{"repo": w.repo}
	}
	return map[string]any{}
}

func connect(ctx context.Context, url string) (*mcp.ClientSession, error) {
	client := mcp.NewClient(&mcp.Implementation{Name: "kg-load", Version: "0.1.0"}, nil)
	transport := &mcp.StreamableClientTransport{Endpoint: url, HTTPClient: &http.Client{Timeout: 60 * time.Second}}
	return client.Connect(ctx, transport, nil)
}

func structured(res *mcp.CallToolResult) map[string]any {
	var out map[string]any
	b, _ := json.Marshal(res.StructuredContent)
	json.Unmarshal(b, &out)
	return out
}

// discover fills the workload from the graph with one session.
func discover(ctx context.Context, cs *mcp.ClientSession, w *workload, prefix string) error {
	res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "find_symbol",
		Arguments: map[string]any{"name": prefix + "*", "repo": w.repo, "limit": 50}})
	if err != nil {
		return err
	}
	if rows, ok := structured(res)["results"].([]any); ok {
		for _, r := range rows {
			if m, ok := r.(map[string]any); ok {
				w.symbols = append(w.symbols, map[string]any{"id": m["id"], "name": m["qualname"]})
			}
		}
	}
	if len(w.symbols) == 0 {
		return fmt.Errorf("find_symbol %s* in %q found nothing; set -repo and -symbol-prefix", prefix, w.repo)
	}
	res, err = cs.CallTool(ctx, &mcp.CallToolParams{Name: "search_knowledge",
		Arguments: map[string]any{"query": w.queries[0], "kind": "Ticket", "k": 20}})
	if err == nil && !res.IsError {
		if rows, ok := structured(res)["results"].([]any); ok {
			for _, r := range rows {
				if m, ok := r.(map[string]any); ok && m["key"] != nil {
					w.tickets = append(w.tickets, fmt.Sprint(m["key"]))
				}
			}
		}
	}
	return nil
}

func percentile(sorted []time.Duration, p float64) float64 {
	if len(sorted) == 0 {
		return 0
	}
	i := int(float64(len(sorted)-1) * p)
	return float64(sorted[i].Microseconds()) / 1000
}

func run(ctx context.Context, url string, w *workload, tools []string, clients int, duration time.Duration) ([]Stat, error) {
	var mu sync.Mutex
	lat := map[string][]time.Duration{}
	errs := map[string]int{}
	sessions := make([]*mcp.ClientSession, clients)
	for i := range sessions {
		cs, err := connect(ctx, url)
		if err != nil {
			return nil, fmt.Errorf("client %d: %w", i, err)
		}
		defer cs.Close()
		sessions[i] = cs
	}
	deadline := time.Now().Add(duration)
	var wg sync.WaitGroup
	for i, cs := range sessions {
		wg.Add(1)
		go func(i int, cs *mcp.ClientSession) {
			defer wg.Done()
			r := rand.New(rand.NewPCG(uint64(i), uint64(clients)))
			for n := i; time.Now().Before(deadline); n++ {
				tool := tools[n%len(tools)]
				t0 := time.Now()
				res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: tool, Arguments: w.args(tool, r)})
				d := time.Since(t0)
				mu.Lock()
				lat[tool] = append(lat[tool], d)
				if err != nil || res.IsError {
					errs[tool]++
				}
				mu.Unlock()
			}
		}(i, cs)
	}
	wg.Wait()
	var out []Stat
	for _, tool := range tools {
		ds := lat[tool]
		sort.Slice(ds, func(a, b int) bool { return ds[a] < ds[b] })
		var sum time.Duration
		for _, d := range ds {
			sum += d
		}
		mean := 0.0
		if len(ds) > 0 {
			mean = float64(sum.Microseconds()) / 1000 / float64(len(ds))
		}
		out = append(out, Stat{Tool: tool, Clients: clients, Calls: len(ds), Errors: errs[tool],
			P50ms: percentile(ds, .50), P95ms: percentile(ds, .95), P99ms: percentile(ds, .99), MeanMs: mean,
			PerSec: float64(len(ds)) / duration.Seconds()})
	}
	return out, nil
}

func main() {
	url := flag.String("url", "http://localhost:8000/mcp", "MCP Streamable HTTP endpoint")
	levels := flag.String("clients", "1,10,50", "comma-separated numbers of concurrent clients")
	duration := flag.Duration("duration", 30*time.Second, "test time per concurrency level")
	repo := flag.String("repo", "scale-r000", "repository for code tools")
	prefix := flag.String("symbol-prefix", "Service", "find_symbol prefix used to collect symbol ids")
	toolList := flag.String("tools", "search_graph,find_symbol,get_symbol_code,impact_analysis,get_neighbors,"+
		"find_related_tests,search_knowledge,get_context_for_ticket,get_change_history", "tools to call, in turn")
	queries := flag.String("queries", "refund an order payment|invite a user to an account|retry a failed invoice|"+
		"export customer data|schedule a shipment", "|-separated questions for the search tools")
	out := flag.String("out", "", "write the results as JSON to this file")
	flag.Parse()

	ctx := context.Background()
	w := &workload{repo: *repo, queries: strings.Split(*queries, "|")}
	cs, err := connect(ctx, *url)
	if err != nil {
		log.Fatalf("connect %s: %v", *url, err)
	}
	if err := discover(ctx, cs, w, *prefix); err != nil {
		log.Fatal(err)
	}
	cs.Close()
	log.Printf("workload: %d symbols, %d tickets, %d queries", len(w.symbols), len(w.tickets), len(w.queries))

	var all []Stat
	var tools []string
	for _, t := range strings.Split(*toolList, ",") {
		if t == "get_context_for_ticket" && len(w.tickets) == 0 {
			log.Printf("no tickets found: %s left out", t)
			continue
		}
		tools = append(tools, t)
	}
	for _, l := range strings.Split(*levels, ",") {
		n, err := strconv.Atoi(strings.TrimSpace(l))
		if err != nil || n < 1 {
			log.Fatalf("bad -clients value %q", l)
		}
		stats, err := run(ctx, *url, w, tools, n, *duration)
		if err != nil {
			log.Fatal(err)
		}
		all = append(all, stats...)
	}
	fmt.Println("| Tool | Clients | Calls | Errors | p50 ms | p95 ms | p99 ms | Calls/s |")
	fmt.Println("|---|---|---|---|---|---|---|---|")
	for _, s := range all {
		fmt.Printf("| %s | %d | %d | %d | %.1f | %.1f | %.1f | %.1f |\n", s.Tool, s.Clients, s.Calls, s.Errors, s.P50ms, s.P95ms, s.P99ms, s.PerSec)
	}
	if *out != "" {
		b, _ := json.MarshalIndent(all, "", "  ")
		if err := os.WriteFile(*out, b, 0o644); err != nil {
			log.Fatal(err)
		}
	}
}
