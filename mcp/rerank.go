package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Reranker calls a cross-encoder through the Text Embeddings Inference /rerank API
// (BAAI/bge-reranker-v2-m3). It reorders the top RRF candidates of search_graph and
// search_knowledge. A nil *Reranker (RERANK_URL unset) is off and keeps the RRF order.
type Reranker struct {
	URL    string
	Model  string // informational: TEI serves one model; reported in results
	APIKey string
	Pool   int // how many RRF candidates to rerank
	client *http.Client
}

// rerankBatch is the TEI default for --max-client-batch-size.
const rerankBatch = 32

// rerankChars caps each passage; the cross-encoder truncates to 512 tokens anyway.
const rerankChars = 2000

func NewReranker(url, model, key string, pool int) *Reranker {
	if url == "" {
		return nil
	}
	return &Reranker{URL: strings.TrimRight(url, "/"), Model: model, APIKey: key, Pool: max(pool, 1),
		client: &http.Client{Timeout: 30 * time.Second}}
}

func rerankerFromEnv() *Reranker {
	pool, _ := strconv.Atoi(env("RERANK_CANDIDATES", "30"))
	return NewReranker(os.Getenv("RERANK_URL"), env("RERANK_MODEL", "BAAI/bge-reranker-v2-m3"), os.Getenv("RERANK_API_KEY"), pool)
}

func (r *Reranker) Enabled() bool { return r != nil && r.URL != "" }

// Scores returns one relevance score per text, in the order of texts.
func (r *Reranker) Scores(ctx context.Context, query string, texts []string) ([]float64, error) {
	out := make([]float64, len(texts))
	for start := 0; start < len(texts); start += rerankBatch {
		batch := texts[start:min(len(texts), start+rerankBatch)]
		for i, t := range batch {
			if len(t) > rerankChars {
				batch[i] = t[:rerankChars]
			}
		}
		body, _ := json.Marshal(map[string]any{"query": query, "texts": batch, "truncate": true, "raw_scores": false})
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, r.URL+"/rerank", bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		if r.APIKey != "" {
			req.Header.Set("Authorization", "Bearer "+r.APIKey)
		}
		resp, err := r.client.Do(req)
		if err != nil {
			return nil, fmt.Errorf("rerank: %w", err)
		}
		var res []struct {
			Index int     `json:"index"`
			Score float64 `json:"score"`
		}
		err = json.NewDecoder(resp.Body).Decode(&res)
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("rerank: HTTP %d", resp.StatusCode)
		}
		if err != nil {
			return nil, fmt.Errorf("rerank: %w", err)
		}
		if len(res) != len(batch) {
			return nil, fmt.Errorf("rerank: %d scores for %d texts", len(res), len(batch))
		}
		for _, x := range res {
			if x.Index < 0 || x.Index >= len(batch) {
				return nil, fmt.Errorf("rerank: index %d out of range", x.Index)
			}
			out[start+x.Index] = x.Score
		}
	}
	return out, nil
}

// Rerank reorders candidates by cross-encoder score and keeps the top k. text gives the passage
// for each id. On any reranker error it logs and returns the RRF order: search must not fail
// because an optional model server is down.
func (r *Reranker) Rerank(ctx context.Context, query string, hits []scored, k int, text func(context.Context, []string) (map[string]string, error)) []scored {
	if !r.Enabled() || len(hits) < 2 {
		return hits[:min(len(hits), k)]
	}
	pool := hits[:min(len(hits), max(r.Pool, k))]
	idList := make([]string, len(pool))
	for i, h := range pool {
		idList[i] = h.ID
	}
	texts, err := text(ctx, idList)
	if err == nil {
		passages := make([]string, len(pool))
		for i, id := range idList {
			passages[i] = texts[id]
		}
		var scores []float64
		if scores, err = r.Scores(ctx, query, passages); err == nil {
			out := make([]scored, len(pool))
			for i, h := range pool {
				out[i] = scored{ID: h.ID, Score: scores[i]}
			}
			sort.SliceStable(out, func(i, j int) bool { return out[i].Score > out[j].Score })
			return out[:min(len(out), k)]
		}
	}
	log.Printf("warning: reranker off for this query: %v", err)
	return hits[:min(len(hits), k)]
}
