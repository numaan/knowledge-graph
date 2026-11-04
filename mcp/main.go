// kg-mcp: read-only MCP server over the knowledge graph for coding agents.
//
//	kg-mcp                      stdio (Claude Code, Cursor, Codex)
//	kg-mcp -http :8000          Streamable HTTP at /mcp and REST at /api (Kubernetes)
//
// Environment: NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, EMBEDDINGS_URL, EMBEDDINGS_MODEL,
// EMBEDDINGS_API_KEY, KG_DEFAULT_REPO, MCP_HTTP_ADDR (same as -http), RERANK_URL, RERANK_MODEL,
// LLM_BASE_URL, LLM_MODEL, LLM_API_KEY (the ask tool). Governance: KG_ACL_GROUPS,
// KG_ACL_MAPPING, KG_OIDC_* (auth.go), KG_AUDIT, KG_AUDIT_FILE (audit.go), KG_WRITEBACK.
// Tracing: OTEL_EXPORTER_OTLP_ENDPOINT (telemetry.go).
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

var version = "0.1.0"

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func newMCPServer(s *Server) *mcp.Server {
	m := mcp.NewServer(&mcp.Implementation{Name: "kg", Title: "Knowledge graph", Version: version}, &mcp.ServerOptions{
		Instructions: "Knowledge graph of code, domain concepts and requirements. Start with search_graph for a question, " +
			"domain_overview for the business view, impact_analysis before you change shared code, find_related_tests " +
			"to know what to run, and trace_requirement to see whether a requirement is implemented. Edges carry " +
			"method (parser, scip, treesitter, llm) and confidence: treat llm and low-confidence edges as hints.",
	})
	s.register(m)
	m.AddReceivingMiddleware(s.accessMiddleware)
	m.AddReceivingMiddleware(observe) // spans and metrics per tool call; wraps the access check
	return m
}

func main() {
	httpAddr := flag.String("http", os.Getenv("MCP_HTTP_ADDR"), "serve Streamable HTTP on this address (e.g. :8000) instead of stdio")
	flag.Parse()
	log.SetOutput(os.Stderr) // stdout belongs to the MCP protocol in stdio mode

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	defer setupTracing(ctx)(context.Background())

	graph, err := NewGraph(env("NEO4J_URI", "bolt://localhost:7687"), env("NEO4J_USER", "neo4j"), os.Getenv("NEO4J_PASSWORD"))
	if err != nil {
		log.Fatalf("neo4j: %v", err)
	}
	defer graph.Close(context.Background())
	vctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	if err := graph.Verify(vctx); err != nil {
		log.Printf("warning: neo4j not reachable yet: %v", err)
	}
	cancel()

	s := &Server{
		graph:       graph,
		emb:         NewEmbedder(env("EMBEDDINGS_URL", "http://localhost:8080"), env("EMBEDDINGS_MODEL", "Qwen/Qwen3-Embedding-0.6B"), os.Getenv("EMBEDDINGS_API_KEY")),
		defaultRepo: os.Getenv("KG_DEFAULT_REPO"),
		groups:      aclGroups(),
		rerank:      rerankerFromEnv(),
		llm:         llmFromEnv(),
	}
	graph.fixed["repos"] = repoScope(s.groups)
	oidc := oidcFromEnv()
	if err := s.setupGovernance(*httpAddr == "", oidc); err != nil {
		log.Fatal(err)
	}
	if oidc != nil && *httpAddr != "" {
		graph.fixed["repos"] = []string{} // a query without a caller sees no repository
	}
	server := newMCPServer(s)

	if *httpAddr == "" {
		if err := server.Run(ctx, &mcp.StdioTransport{}); err != nil {
			log.Fatal(err)
		}
		return
	}
	mux := http.NewServeMux()
	var handler http.Handler = mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, nil)
	if oidc != nil {
		handler = oidc.Middleware(handler)
		mux.Handle(metadataPath, oidc.MetadataHandler())
		mux.Handle(metadataPath+"/mcp", oidc.MetadataHandler())
		log.Printf("OIDC: tokens from %s for audience %s", oidc.Issuer, oidc.Audience)
	} else {
		log.Printf("warning: KG_OIDC_ISSUER is not set; every HTTP caller gets KG_ACL_GROUPS")
	}
	mux.Handle("/mcp", handler)
	api, err := newREST(ctx, s, server) // REST for the web UI; see rest.go
	if err != nil {
		log.Fatalf("rest: %v", err)
	}
	api.Feedback = s.feedbackSink() // feedback becomes a :Proposal for kg-review
	if oidc != nil {
		api.Identity = oidc.Caller // same bearer token check as /mcp
	}
	api.Mount(mux)
	mux.Handle("/metrics", metricsHandler())
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		if err := graph.Verify(r.Context()); err != nil {
			http.Error(w, err.Error(), http.StatusServiceUnavailable)
			return
		}
		w.Write([]byte("ok"))
	})
	srv := &http.Server{Addr: *httpAddr, Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	go func() { <-ctx.Done(); srv.Shutdown(context.Background()) }()
	log.Printf("kg-mcp %s listening on %s/mcp", version, *httpAddr)
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
