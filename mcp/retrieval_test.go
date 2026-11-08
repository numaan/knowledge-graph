package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"slices"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestRouter(t *testing.T) {
	cases := []struct{ q, intent string }{
		{"where are users invited in bulk?", IntentCode},
		{"which file defines the scenario store", IntentCode},
		{"what breaks if I change create_invitation?", IntentImpact},
		{"who calls ScenariosView.onSave", IntentImpact},
		{"why are scenarios not kept in local storage?", IntentHistory},
		{"when was the retry logic introduced", IntentHistory},
		{"give me the context for PREC-123", IntentTicket},
		{"is FR-P10-03-02 implemented?", IntentRequirement},
		{"does the code satisfy the requirement that admins approve invites", IntentRequirement},
		{"explain the architecture of the simulation engine", IntentOverview},
		{"what is a fault type", IntentOverview},
		{"who wrote the scoring module", IntentPeople},
		{"who owns scenarioStore.ts", IntentPeople},
		{"scoring weights", IntentCode}, // no rule: default
	}
	for _, c := range cases {
		r := routeByRules(c.q)
		if r.Intent != c.intent {
			t.Errorf("%q: intent %s (%v), want %s", c.q, r.Intent, r.Confidence, c.intent)
		}
		if len(r.Tools) == 0 {
			t.Errorf("%q: no tools", c.q)
		}
	}
	r := routeByRules("is FR-P10-03-02 implemented? see PREC-7 and `ScenariosView.onSave`")
	if !slices.Equal(r.Requirements, []string{"FR-P10-03-02"}) || !slices.Equal(r.Tickets, []string{"PREC-7"}) {
		t.Errorf("entities: reqs %v tickets %v", r.Requirements, r.Tickets)
	}
	if !slices.Contains(r.Symbols, "ScenariosView.onSave") {
		t.Errorf("symbols: %v", r.Symbols)
	}
	if got := routeByRules("feature P14-02 status"); len(got.Tickets) != 0 || len(got.Requirements) != 1 {
		t.Errorf("feature id must be a requirement, not a ticket: %+v", got)
	}
	if got := codeNames("use e.g. the bulk_invite handler in app/routers/users.py"); !slices.Equal(got, []string{"bulk_invite", "app/routers/users.py"}) {
		t.Errorf("codeNames: %v", got)
	}
	// "why ... who calls": the second intent adds its tools.
	r = routeByRules("why does nobody call ScenariosView.onSave any more, and who calls it now?")
	if !slices.Contains(r.Tools, "impact_analysis") || !slices.Contains(r.Tools, "get_change_history") {
		t.Errorf("mixed question tools: %+v", r)
	}
}

func TestRouterLLMFallback(t *testing.T) {
	var calls atomic.Int32
	llm := mockLLM(t, &calls, func(string) string { return "history" })
	r := routeQuestion(context.Background(), llm, true, "scoring weights")
	if r.Intent != IntentHistory || r.Method != "llm" || calls.Load() != 1 {
		t.Errorf("fallback: %+v after %d calls", r, calls.Load())
	}
	// confident rules do not call the LLM
	routeQuestion(context.Background(), llm, true, "what breaks if I change create_invitation?")
	if calls.Load() != 1 {
		t.Error("LLM called for a confident rules result")
	}
	if r := routeQuestion(context.Background(), llm, false, "scoring weights"); r.Method != "default" {
		t.Errorf("useLLM=false: %+v", r)
	}
}

func TestRerankClient(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/rerank" || r.Header.Get("Authorization") != "Bearer k" {
			http.Error(w, "bad request", 400)
			return
		}
		var in struct {
			Query string   `json:"query"`
			Texts []string `json:"texts"`
		}
		json.NewDecoder(r.Body).Decode(&in)
		if len(in.Texts) > rerankBatch || in.Query != "invite users" {
			http.Error(w, "bad batch", 413)
			return
		}
		// score = length of the text; TEI returns results sorted by score
		out := []map[string]any{}
		for i, x := range in.Texts {
			out = append(out, map[string]any{"index": i, "score": float64(len(x)) / 1000})
		}
		slices.SortFunc(out, func(a, b map[string]any) int { return int(b["score"].(float64)*1000 - a["score"].(float64)*1000) })
		json.NewEncoder(w).Encode(out)
	}))
	defer srv.Close()

	r := NewReranker(srv.URL+"/", "BAAI/bge-reranker-v2-m3", "k", 40)
	hits := make([]scored, 40)
	texts := map[string]string{}
	for i := range hits {
		id := fmt.Sprintf("c%02d", i)
		hits[i] = scored{ID: id, Score: 1 / float64(i+1)}
		texts[id] = strings.Repeat("x", i+1) // the last RRF hit has the longest text
	}
	text := func(_ context.Context, ids []string) (map[string]string, error) { return texts, nil }
	got := r.Rerank(context.Background(), "invite users", hits, 5, text)
	if len(got) != 5 || got[0].ID != "c39" || got[4].ID != "c35" || got[0].Score != 0.04 {
		t.Fatalf("rerank order: %+v", got)
	}
	if calls.Load() != 2 {
		t.Errorf("40 texts must take 2 batches, took %d", calls.Load())
	}
	// errors fall back to the RRF order
	bad := NewReranker(srv.URL, "", "wrong", 40)
	if got := bad.Rerank(context.Background(), "invite users", hits, 3, text); got[0].ID != "c00" || len(got) != 3 {
		t.Errorf("fallback: %+v", got)
	}
	var off *Reranker
	if off.Enabled() || NewReranker("", "", "", 1) != nil {
		t.Error("empty RERANK_URL must turn the reranker off")
	}
	if got := off.Rerank(context.Background(), "q", hits, 2, text); len(got) != 2 || got[0].ID != "c00" {
		t.Errorf("off: %+v", got)
	}
}

// mockLLM is an OpenAI-compatible chat endpoint; reply gets the user message.
func mockLLM(t *testing.T, calls *atomic.Int32, reply func(user string) string) *ChatLLM {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/v1/chat/completions" {
			http.NotFound(w, r)
			return
		}
		var in struct {
			Model    string `json:"model"`
			Messages []struct{ Role, Content string }
		}
		json.NewDecoder(r.Body).Decode(&in)
		json.NewEncoder(w).Encode(map[string]any{"choices": []any{map[string]any{"message": map[string]any{
			"role": "assistant", "content": reply(in.Messages[len(in.Messages)-1].Content)}}}})
	}))
	t.Cleanup(srv.Close)
	return NewChatLLM(srv.URL, "test-model", "")
}

func TestAskAnswer(t *testing.T) {
	var calls atomic.Int32
	var prompt string
	llm := mockLLM(t, &calls, func(user string) string {
		prompt = user
		return "<think>plan</think>Users are invited in bulk by bulk_invite [1], called from the users view [2, 9]. " +
			"The ADR explains why [7]."
	})
	llm.CacheDir = t.TempDir()
	items := []evidence{
		{ID: "precept:app/routers/users.py#bulk_invite", Label: "Function", Title: "bulk_invite", Path: "app/routers/users.py:10-40", Text: "def bulk_invite(...)"},
		{ID: "precept:src/UsersView.tsx#UsersView.onBulkInvite", Label: "Method", Title: "UsersView.onBulkInvite", Path: "src/UsersView.tsx:5-9", Text: "onBulkInvite() {}"},
		{ID: "confluence:ENG:42#s1", Label: "Section", Title: "Invites", URL: "https://wiki/x", Text: "Invites are sent in batches."},
	}
	route := routeByRules("where are users invited in bulk?")
	out, err := answer(context.Background(), llm, "where are users invited in bulk?", route, items)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(prompt, "[3] Section Invites (id: confluence:ENG:42#s1, https://wiki/x)") {
		t.Errorf("prompt does not number the context:\n%s", prompt)
	}
	if strings.Contains(out.Answer, "think") || strings.Contains(out.Answer, "[7]") || strings.Contains(out.Answer, "9") {
		t.Errorf("answer keeps thinking or invalid citations: %q", out.Answer)
	}
	if !strings.Contains(out.Answer, "[2]") || !strings.HasSuffix(out.Answer, "explains why.") {
		t.Errorf("answer: %q", out.Answer)
	}
	if len(out.Citations) != 2 || out.Citations[0].ID != items[0].ID || out.Citations[1].Path != items[1].Path {
		t.Errorf("citations: %+v", out.Citations)
	}
	if !slices.Equal(out.InvalidCitations, []int{7, 9}) || out.Note == "" {
		t.Errorf("invalid citations: %v, note %q", out.InvalidCitations, out.Note)
	}
	if out.Confidence <= 0 || out.Confidence >= 0.8 {
		t.Errorf("confidence with invalid citations: %v", out.Confidence)
	}
	// the cache answers the same prompt without a call
	again, err := answer(context.Background(), llm, "where are users invited in bulk?", route, items)
	if err != nil || calls.Load() != 1 || again.Answer != out.Answer {
		t.Errorf("cache: %d calls, err %v", calls.Load(), err)
	}

	// no context: no LLM call
	empty, _ := answer(context.Background(), llm, "anything", route, nil)
	if calls.Load() != 1 || empty.Confidence != 0 || len(empty.Citations) != 0 {
		t.Errorf("empty context: %+v", empty)
	}

	// an answer that says the context is not enough has low confidence
	unsure := mockLLM(t, &calls, func(string) string { return "The context does not contain the retry policy [1]." })
	low, _ := answer(context.Background(), unsure, "retry policy?", route, items)
	if low.Confidence > 0.25 {
		t.Errorf("unsure answer confidence %v", low.Confidence)
	}
}

func TestBuildPromptBudget(t *testing.T) {
	big := strings.Repeat("y", askItemChars*2)
	var items []evidence
	for i := range 40 {
		items = append(items, evidence{ID: fmt.Sprint(i), Label: "Function", Title: "f", Text: big})
	}
	p, kept := buildPrompt("q", items)
	if len(p) > askContextChars+500 || len(kept) == 0 || len(kept) == len(items) {
		t.Errorf("prompt %d chars, %d items kept", len(p), len(kept))
	}
	if !strings.Contains(p, "…[cut") {
		t.Error("long item not cut")
	}
}

func TestAskACL(t *testing.T) {
	team := &Server{groups: map[string]bool{"repo:precept": true, "jira:PREC": true}}
	if team.allowed(context.Background(), []any{"jira:HR"}) || !team.allowed(context.Background(), []any{"repo:precept"}) {
		t.Fatal("ACL check")
	}
	if fileOf("precept:src/a.ts#A.b") != "precept:src/a.ts" || fileOf("precept:src/a.ts") != "precept:src/a.ts" {
		t.Error("fileOf")
	}
	// without an LLM the ask tool is not registered
	m := mcp.NewServer(&mcp.Implementation{Name: "t", Version: "0"}, nil)
	(&Server{groups: map[string]bool{"*": true}}).registerAsk(m)
	cs := connect(t, m)
	tools, err := cs.ListTools(context.Background(), nil)
	if err != nil || len(tools.Tools) != 0 {
		t.Errorf("ask registered without LLM: %v %v", tools, err)
	}
}

func connect(t *testing.T, m *mcp.Server) *mcp.ClientSession {
	t.Helper()
	st, ct := mcp.NewInMemoryTransports()
	if _, err := m.Connect(context.Background(), st, nil); err != nil {
		t.Fatal(err)
	}
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil).Connect(context.Background(), ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cs.Close() })
	return cs
}

type pageTestIn struct {
	Page
	Query string `json:"query"`
}

func TestPagination(t *testing.T) {
	rows := make([]map[string]any, 50)
	for i := range rows {
		rows[i] = map[string]any{"id": fmt.Sprintf("n%02d", i), "text": strings.Repeat("z", 400)}
	}
	m := mcp.NewServer(&mcp.Implementation{Name: "t", Version: "0"}, nil)
	addTool(m, &mcp.Tool{Name: "list"}, func(_ context.Context, _ *mcp.CallToolRequest, in pageTestIn) (*mcp.CallToolResult, Rows, error) {
		return nil, Rows{Results: slices.Clone(rows), Note: "test"}, nil
	})
	addTool(m, &mcp.Tool{Name: "doc"}, func(_ context.Context, _ *mcp.CallToolRequest, in pageTestIn) (*mcp.CallToolResult, Doc, error) {
		return nil, Doc{Data: map[string]any{"code": strings.Repeat("c", 50000), "affected": slices.Clone(rows), "n": 1}}, nil
	})
	cs := connect(t, m)
	call := func(name string, args map[string]any) (map[string]any, bool) {
		t.Helper()
		res, err := cs.CallTool(context.Background(), &mcp.CallToolParams{Name: name, Arguments: args})
		if err != nil {
			t.Fatal(err)
		}
		var out map[string]any
		b, _ := json.Marshal(res.StructuredContent)
		json.Unmarshal(b, &out)
		return out, res.IsError
	}

	seen := []string{}
	args := map[string]any{"query": "q", "max_chars": 5000}
	for page := 0; page < 20; page++ {
		out, isErr := call("list", args)
		if isErr {
			t.Fatalf("page %d: error %v", page, out)
		}
		res := out["results"].([]any)
		b, _ := json.Marshal(out)
		if len(b) > 5000 || len(res) == 0 {
			t.Fatalf("page %d: %d chars, %d rows", page, len(b), len(res))
		}
		for _, r := range res {
			seen = append(seen, r.(map[string]any)["id"].(string))
		}
		if out["total"].(float64) != 50 || out["truncated"] != true || !strings.HasPrefix(out["note"].(string), "test; rows ") {
			t.Fatalf("page %d: %v %v %v", page, out["total"], out["truncated"], out["note"])
		}
		next, _ := out["next_cursor"].(string)
		if next == "" {
			break
		}
		args["cursor"] = next
	}
	if len(seen) != 50 || seen[0] != "n00" || seen[49] != "n49" || len(slices.Compact(slices.Clone(seen))) != 50 {
		t.Fatalf("pages returned %d rows: %v", len(seen), seen)
	}
	// a cursor is bound to its arguments
	args["query"] = "other"
	if _, isErr := call("list", args); !isErr {
		t.Error("cursor accepted for other arguments")
	}
	if _, isErr := call("list", map[string]any{"query": "q", "cursor": "garbage"}); !isErr {
		t.Error("garbage cursor accepted")
	}
	// default budget: all rows fit in one page, no cursor
	if out, _ := call("list", map[string]any{"query": "q", "max_chars": 100000}); out["next_cursor"] != nil || out["truncated"] != nil {
		t.Errorf("small result paged: %v", out["note"])
	}
	// one row larger than the budget is cut, so the page still makes progress
	if out, _ := call("list", map[string]any{"query": "q", "max_chars": 1000}); len(out["results"].([]any)) != 1 {
		t.Errorf("tiny budget: %v", out)
	}

	doc, isErr := call("doc", map[string]any{"query": "q", "max_chars": 8000})
	b, _ := json.Marshal(doc)
	if isErr || len(b) > 8000 || doc["truncated"] != true {
		t.Fatalf("doc: %d chars, %v", len(b), doc["truncated"])
	}
	data := doc["data"].(map[string]any)
	if !strings.Contains(data["code"].(string), "…[cut ") || data["n"].(float64) != 1 {
		t.Errorf("doc string not marked: %.80s", data["code"])
	}
	if om := doc["omitted"].(map[string]any); om["affected"].(float64) != float64(50-len(data["affected"].([]any))) {
		t.Errorf("omitted: %v", om)
	}
	if _, isErr := call("doc", map[string]any{"query": "q", "cursor": encodeCursor(argsKey(pageTestIn{Query: "q"}), 1)}); !isErr {
		t.Error("cursor accepted for a document tool")
	}
}

func TestCutString(t *testing.T) {
	s := "héllo wörld"
	got := cutString(s, 2) // inside the 2-byte é
	if !strings.HasPrefix(got, "h…[cut ") {
		t.Errorf("cut %q", got)
	}
	if cutString("short", 10) != "short" {
		t.Error("short string changed")
	}
}

// Integration test for ask: real graph, mock LLM that cites the first two items.
func TestAskIntegration(t *testing.T) {
	if os.Getenv("NEO4J_PASSWORD") == "" {
		t.Skip("NEO4J_PASSWORD not set")
	}
	ctx := context.Background()
	graph, err := NewGraph(env("NEO4J_URI", "bolt://localhost:7687"), env("NEO4J_USER", "neo4j"), os.Getenv("NEO4J_PASSWORD"))
	if err != nil {
		t.Fatal(err)
	}
	defer graph.Close(ctx)
	if err := graph.Verify(ctx); err != nil {
		t.Skip("neo4j not reachable:", err)
	}
	var calls atomic.Int32
	s := &Server{graph: graph, emb: NewEmbedder(env("EMBEDDINGS_URL", "http://localhost:11434"),
		env("EMBEDDINGS_MODEL", "qwen3-embedding:0.6b"), ""), defaultRepo: env("KG_TEST_REPO", "precept"),
		groups: map[string]bool{"*": true}, rerank: rerankerFromEnv(),
		llm: mockLLM(t, &calls, func(string) string { return "See [1] and [2]." })}
	cs := connect(t, newMCPServer(s))
	res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "ask", Arguments: map[string]any{"question": "where are users invited in bulk?"}})
	if err != nil || res.IsError {
		t.Fatalf("ask: %v %+v", err, res)
	}
	var out AskOut
	b, _ := json.Marshal(res.StructuredContent)
	json.Unmarshal(b, &out)
	if len(out.Citations) != 2 || out.Citations[0].ID == "" || !slices.Contains(out.ToolsUsed, "search_graph") {
		t.Errorf("ask: %+v", out)
	}
}

// Every read tool takes max_chars and cursor; ask is registered only with an LLM.
func TestToolSchemas(t *testing.T) {
	s := &Server{groups: map[string]bool{"*": true}, llm: NewChatLLM("http://localhost:1", "m", "")}
	tools, err := connect(t, newMCPServer(s)).ListTools(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(tools.Tools) != 22 {
		t.Errorf("want 16 read tools + 3 write-back tools + ask + 2 GraphRAG tools, got %d", len(tools.Tools))
	}
	unpaged := map[string]bool{"ask": true, "global_search": true, "local_search": true, "report_wrong_fact": true, "record_decision": true, "suggest_link": true}
	for _, tool := range tools.Tools {
		b, _ := json.Marshal(tool.InputSchema)
		paged := strings.Contains(string(b), `"max_chars"`) && strings.Contains(string(b), `"cursor"`)
		if paged == unpaged[tool.Name] {
			t.Errorf("%s: max_chars and cursor in schema = %v", tool.Name, paged)
		}
	}
}
