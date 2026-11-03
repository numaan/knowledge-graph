package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

// ChatLLM calls an OpenAI-compatible /v1/chat/completions endpoint (Ollama, vLLM, OpenAI).
// Answers are cached by a hash of model and prompt in LLM_CACHE_DIR when it is set, so a repeated
// question makes no LLM call.
type ChatLLM struct {
	URL       string // full chat completions URL
	Model     string
	APIKey    string
	Reasoning string // optional reasoning_effort, e.g. "none" for Ollama thinking models
	CacheDir  string
	client    *http.Client
}

// NewChatLLM takes a base URL such as http://localhost:11434 or https://api.example.com/v1.
func NewChatLLM(base, model, key string) *ChatLLM {
	base = strings.TrimRight(base, "/")
	if !strings.HasSuffix(base, "/v1") {
		base += "/v1"
	}
	return &ChatLLM{URL: base + "/chat/completions", Model: model, APIKey: key, client: &http.Client{Timeout: 5 * time.Minute}}
}

// llmFromEnv is nil (ask off) unless LLM_BASE_URL and LLM_MODEL are set and KG_ASK is not off.
func llmFromEnv() *ChatLLM {
	if os.Getenv("LLM_BASE_URL") == "" || os.Getenv("LLM_MODEL") == "" || os.Getenv("KG_ASK") == "off" {
		return nil
	}
	l := NewChatLLM(os.Getenv("LLM_BASE_URL"), os.Getenv("LLM_MODEL"), os.Getenv("LLM_API_KEY"))
	l.Reasoning = os.Getenv("LLM_REASONING_EFFORT")
	l.CacheDir = os.Getenv("LLM_CACHE_DIR")
	return l
}

var thinkRe = regexp.MustCompile(`(?s)<think>.*?</think>`)

// Complete sends one system and one user message at temperature 0 and returns the answer text.
func (l *ChatLLM) Complete(ctx context.Context, system, user string, maxTokens int) (string, error) {
	body := map[string]any{"model": l.Model, "temperature": 0, "stream": false, "max_tokens": maxTokens,
		"messages": []map[string]string{{"role": "system", "content": system}, {"role": "user", "content": user}}}
	if l.Reasoning != "" {
		body["reasoning_effort"] = l.Reasoning
	}
	raw, _ := json.Marshal(body)
	cache := ""
	if l.CacheDir != "" {
		sum := sha256.Sum256(raw)
		cache = filepath.Join(l.CacheDir, "ask-"+hex.EncodeToString(sum[:16])+".json")
		if b, err := os.ReadFile(cache); err == nil {
			var c struct{ Content string }
			if json.Unmarshal(b, &c) == nil {
				return c.Content, nil
			}
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, l.URL, bytes.NewReader(raw))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	if l.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+l.APIKey)
	}
	resp, err := l.client.Do(req)
	if err != nil {
		return "", fmt.Errorf("llm: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("llm: HTTP %d", resp.StatusCode)
	}
	var out struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", fmt.Errorf("llm: %w", err)
	}
	if len(out.Choices) == 0 {
		return "", fmt.Errorf("llm: empty response")
	}
	text := strings.TrimSpace(thinkRe.ReplaceAllString(out.Choices[0].Message.Content, ""))
	if cache != "" && text != "" {
		if os.MkdirAll(l.CacheDir, 0o755) == nil {
			b, _ := json.Marshal(map[string]string{"content": text})
			os.WriteFile(cache, b, 0o644)
		}
	}
	return text, nil
}
