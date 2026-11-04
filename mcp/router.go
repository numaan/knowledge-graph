package main

import (
	"context"
	"regexp"
	"slices"
	"strings"
)

// Query router for ask: classify a question with cheap rules first, use the LLM only when the
// rules are not sure (KG_ROUTER_LLM=on), and pick the retrieval tools for the class.

const (
	IntentCode        = "code_location" // where is X done
	IntentImpact      = "impact"        // what breaks if X changes
	IntentHistory     = "history"       // why / when X changed
	IntentTicket      = "ticket"        // context of a Jira ticket
	IntentRequirement = "requirement"   // is a requirement implemented
	IntentOverview    = "overview"      // what is X, how does the system work
	IntentPeople      = "people"        // who wrote or knows X
)

var intents = []string{IntentCode, IntentImpact, IntentHistory, IntentTicket, IntentRequirement, IntentOverview, IntentPeople}

// Retrieval per intent, most important first. "summaries" reads optional :Summary nodes.
var intentTools = map[string][]string{
	IntentCode:        {"search_graph"},
	IntentImpact:      {"search_graph", "impact_analysis"},
	IntentHistory:     {"search_graph", "get_change_history", "search_knowledge"},
	IntentTicket:      {"get_context_for_ticket", "search_knowledge"},
	IntentRequirement: {"trace_requirement", "search_knowledge", "search_graph"},
	IntentOverview:    {"summaries", "domain_overview", "search_knowledge", "search_graph"},
	IntentPeople:      {"search_graph", "get_change_history"},
}

type rule struct {
	intent string
	re     *regexp.Regexp
	weight float64
}

var rules = []rule{
	{IntentImpact, regexp.MustCompile(`(?i)\b(impact|blast radius|(would|will|could|can|might) break|breaks? if|affected|if (i|we) (change|modify|remove|delete|rename)|depend(s|ents|encies)? on|who (calls|uses)|callers? of|used by|safe to (change|remove|delete|rename))\b`), 2},
	{IntentHistory, regexp.MustCompile(`(?i)\b(why|history|when (was|were|did)|introduced|commits?|committed|reason|decision|adr|regression|rationale)\b`), 1.5},
	{IntentHistory, regexp.MustCompile(`(?i)\b(changed|modified|refactored|reverted)\b`), 1},
	{IntentPeople, regexp.MustCompile(`(?i)\b(who (wrote|owns|maintains|knows|worked|changed|is responsible|built|implemented|added)|owners?|maintainers?|experts?|contact person|authors?|reviewers?)\b`), 2.5},
	{IntentRequirement, regexp.MustCompile(`(?i)\b(requirements?|specification|acceptance criteria|is .{1,80} implemented|implemented yet|compliant|compliance|satisf(y|ies|ied)|verdict|meets? the)\b`), 2},
	{IntentOverview, regexp.MustCompile(`(?i)\b(overview|architecture|explain|what (is|are)|what does .{1,60} mean|concepts?|summar(y|ise|ize)|how does .{1,80} work|high[- ]level|big picture|purpose|introduction)\b`), 1.5},
	{IntentCode, regexp.MustCompile(`(?i)\b(where|which (file|files|class|function|method|module|component)|find|locate|defined|implementation|handler|endpoint|code (for|that))\b`), 1.5},
	{IntentTicket, regexp.MustCompile(`(?i)\b(ticket|issue|story|epic|bug report|jira)\b`), 1},
}

var (
	// Requirement references and feature ids: FR-P10-03-02, NFR-12, P14-02.
	reqRefRe = regexp.MustCompile(`\b(?:(?:FR|NFR|BR|REQ|UC)-[A-Z0-9]+(?:-[A-Z0-9]+)*|P\d+-\d+)\b`)
	// Jira keys need two letters first, so feature ids such as P14-02 do not match.
	ticketKeyRe = regexp.MustCompile(`\b[A-Z]{2}[A-Z0-9_]*-\d+\b`)
	fileExtRe   = regexp.MustCompile(`\.(go|py|ts|tsx|js|jsx|java|kt|cs|md|yaml|yml)$`)
	backtickRe  = regexp.MustCompile("`([^`\n]{2,120})`")
	// Code names: CamelCase, snake_case, Class.method, call() and file paths.
	codeNameRe = regexp.MustCompile(`\b(?:[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*|[a-z][a-z0-9]*_[a-z0-9_]+|[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\b(?:\(\))?|[\w./-]+\.(?:go|py|ts|tsx|js|jsx|java|kt|cs|md|yaml|yml)\b`)
)

type Route struct {
	Intent       string   `json:"intent"`
	Confidence   float64  `json:"confidence"`
	Method       string   `json:"method"` // rules, llm or default
	Tools        []string `json:"tools"`
	Tickets      []string `json:"tickets,omitempty"`
	Requirements []string `json:"requirements,omitempty"`
	Symbols      []string `json:"symbols,omitempty"`
}

// routeByRules scores each intent by the rules that match. The second intent adds its tools when
// it scores at least 75 % of the first.
func routeByRules(q string) Route {
	r := Route{Requirements: unique(reqRefRe.FindAllString(q, 5))}
	rest := reqRefRe.ReplaceAllString(q, " ")
	r.Tickets = unique(ticketKeyRe.FindAllString(rest, 5))
	r.Symbols = codeNames(q)

	score := map[string]float64{}
	if len(r.Tickets) > 0 {
		score[IntentTicket] += 3
	}
	if len(r.Requirements) > 0 {
		score[IntentRequirement] += 3
	}
	for _, ru := range rules {
		if ru.re.MatchString(rest) {
			score[ru.intent] += ru.weight
		}
	}
	ranked := slices.Clone(intents)
	slices.SortStableFunc(ranked, func(a, b string) int {
		switch {
		case score[a] > score[b]:
			return -1
		case score[a] < score[b]:
			return 1
		}
		return 0
	})
	top, second := score[ranked[0]], score[ranked[1]]
	if top == 0 {
		r.Intent, r.Confidence, r.Method = IntentCode, 0.3, "default"
		if len(r.Symbols) > 0 {
			r.Confidence = 0.5
		}
		r.Tools = slices.Clone(intentTools[IntentCode])
		return r
	}
	r.Intent, r.Method = ranked[0], "rules"
	r.Confidence = round2(0.5 + 0.5*(top-second)/top)
	r.Tools = slices.Clone(intentTools[r.Intent])
	if second > 0 && second >= 0.75*top {
		for _, t := range intentTools[ranked[1]] {
			if !slices.Contains(r.Tools, t) {
				r.Tools = append(r.Tools, t)
			}
		}
	}
	return r
}

// routeQuestion classifies q. With useLLM, a rules result below 0.6 confidence goes to the LLM, which
// must answer with one intent name; on any LLM error the rules result stands.
func routeQuestion(ctx context.Context, llm *ChatLLM, useLLM bool, q string) Route {
	r := routeByRules(q)
	if !useLLM || llm == nil || r.Confidence >= 0.6 {
		return r
	}
	out, err := llm.Complete(ctx, routerPrompt, "Question: "+q, 20)
	if err != nil {
		return r
	}
	got := strings.ToLower(out)
	for _, in := range intents {
		if strings.Contains(got, in) {
			r.Intent, r.Method, r.Confidence = in, "llm", 0.7
			r.Tools = slices.Clone(intentTools[in])
			if in == IntentTicket && len(r.Tickets) == 0 {
				r.Tools = []string{"search_knowledge"}
			}
			break
		}
	}
	return r
}

const routerPrompt = "Classify a question about a software system. Answer with exactly one word from this list: " +
	"code_location (where something is implemented), impact (what breaks if code changes), history (why or when code " +
	"changed), ticket (a Jira ticket), requirement (whether a requirement is implemented), overview (what a concept " +
	"or part of the system is), people (who wrote, owns or knows code)."

func codeNames(q string) []string {
	var out []string
	for _, m := range backtickRe.FindAllStringSubmatch(q, 5) {
		out = append(out, strings.TrimSuffix(strings.TrimSpace(m[1]), "()"))
	}
	for _, m := range codeNameRe.FindAllString(q, 10) {
		m = strings.TrimSuffix(m, "()")
		// lower-case dotted words are prose ("e.g"), not code, unless they name a file
		if strings.Contains(m, ".") && strings.ToLower(m) == m && !strings.ContainsAny(m, "/_") && !fileExtRe.MatchString(m) {
			continue
		}
		out = append(out, m)
	}
	return unique(out)
}

func unique(xs []string) []string {
	var out []string
	for _, x := range xs {
		if x != "" && !slices.Contains(out, x) {
			out = append(out, x)
		}
	}
	return out
}

func round2(f float64) float64 { return float64(int(f*100+0.5)) / 100 }
