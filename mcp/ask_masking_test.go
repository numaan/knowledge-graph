package main

import (
	"regexp"
	"testing"
)

// TestAskQueriesPreferMaskedText checks that every ask query reading node text prefers the PII-masked
// copy, since that text goes to the external LLM and back out as citation excerpts.
func TestAskQueriesPreferMaskedText(t *testing.T) {
	masked := regexp.MustCompile(`coalesce\((\w+)\.text_masked, (\w+)\.text\)`)
	rawText := regexp.MustCompile(`\b\w+\.text\b`)
	for name, q := range map[string]string{
		"askHistoryCypher": askHistoryCypher,
		"askTicketCypher":  askTicketCypher,
		"askSummaryCypher": askSummaryCypher,
	} {
		m := masked.FindAllStringSubmatch(q, -1)
		if len(m) == 0 {
			t.Errorf("%s: no coalesce(x.text_masked, x.text)", name)
		}
		for _, g := range m {
			if g[1] != g[2] {
				t.Errorf("%s: masked and raw text of different nodes: %s", name, g[0])
			}
		}
		if raw := rawText.FindAllString(masked.ReplaceAllString(q, ""), -1); len(raw) > 0 {
			t.Errorf("%s: reads raw node text %v", name, raw)
		}
	}
}
