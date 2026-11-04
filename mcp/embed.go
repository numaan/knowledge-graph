package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
)

// Qwen3-Embedding expects an instruction on the query side only (documents were embedded without one).
const queryInstruction = "Instruct: Given a question about a software system, retrieve relevant code and domain concepts\nQuery: "

// Embedder calls an OpenAI-compatible /v1/embeddings endpoint (TEI, Ollama, vLLM, OpenAI).
type Embedder struct {
	URL    string
	Model  string
	APIKey string
	client *http.Client
	mu     sync.Mutex
	cache  map[string][]float64 // recent query vectors: ask embeds the same question for several indexes
}

func NewEmbedder(url, model, key string) *Embedder {
	return &Embedder{URL: strings.TrimRight(url, "/"), Model: model, APIKey: key,
		client: &http.Client{Timeout: 60 * time.Second}}
}

func (e *Embedder) Query(ctx context.Context, text string) ([]float64, error) {
	e.mu.Lock()
	vec, ok := e.cache[text]
	e.mu.Unlock()
	if ok {
		return vec, nil
	}
	vec, err := e.query(ctx, text)
	if err == nil {
		e.mu.Lock()
		if e.cache == nil || len(e.cache) >= 256 {
			e.cache = map[string][]float64{}
		}
		e.cache[text] = vec
		e.mu.Unlock()
	}
	return vec, err
}

func (e *Embedder) query(ctx context.Context, text string) (_ []float64, err error) {
	ctx, span, start := childSpan(ctx, "embeddings "+e.Model, attribute.String("gen_ai.operation.name", "embeddings"),
		attribute.String("gen_ai.request.model", e.Model), attribute.String("openinference.span.kind", "EMBEDDING"))
	defer func() { endSpan(span, start, embedSeconds, err) }()
	body, _ := json.Marshal(map[string]any{"model": e.Model, "input": []string{queryInstruction + text}})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, e.URL+"/v1/embeddings", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if e.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+e.APIKey)
	}
	resp, err := e.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("embeddings: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("embeddings: HTTP %d", resp.StatusCode)
	}
	var out struct {
		Data []struct {
			Embedding []float64 `json:"embedding"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("embeddings: %w", err)
	}
	if len(out.Data) == 0 {
		return nil, fmt.Errorf("embeddings: empty response")
	}
	return out.Data[0].Embedding, nil
}
