# End-to-end check on kind (kg-test), 2026-11-04

Branch `integration` (all 8 streams), Helm chart with `values-kind.yaml`, embeddings `qwen3-embedding:0.6b`
in Ollama on the host, LLM off. Test repositories: `pallets/itsdangerous` (through Dagster) and the
`bookshop` fixture (through the CLI, for the evaluation set).

## Results

| Area | Check | Result |
|---|---|---|
| Deploy | 7 pods ready (Neo4j, Postgres, Dagster webserver and daemon, pipeline, MCP, web) | Pass |
| Pipeline | Code assets for itsdangerous: 144 symbols, 123 chunks (all embedded), 38 summaries, 436 commits | Pass |
| Pipeline | Source assets: Jira and Confluence skip (not configured); `people` gives 44 people, 3 of them bots | Pass |
| Pipeline | `github_prs` without a token: fails on the GitHub rate limit (60 calls per hour) | Fixed: needs `GITHUB_TOKEN` now |
| MCP | 19 tools listed; `ask` is off when `llm.enabled=false` | Pass after fix |
| MCP | search_graph, find_symbol, get_symbol_code, impact_analysis, get_neighbors, get_change_history, get_summary (repo and file), domain_overview, read_cypher, search_knowledge, get_owners, find_experts | Pass |
| MCP | Paging: `max_chars` 1000 gives `next_cursor`; the cursor gives the next page | Pass |
| MCP | Write-back: `report_wrong_fact` stores a proposal; a repeat is not stored twice | Pass |
| MCP | `find_related_tests` for `Signer.sign` finds no tests | Limitation: the tests call methods on pytest fixtures, which SCIP and tree-sitter cannot type |
| Access | MCP limited to `repo:bookshop`: itsdangerous symbols, people and `read_cypher` are hidden; bookshop is visible | Pass |
| Web | Home, search and entity pages on deployed data (code, properties, neighbours, change history, feedback buttons) | Pass |
| Eval | `eval/fixture.yaml` with the real embedder: hit@8 93%, hit@3 93%, MRR 0.845, impact recall 100%, 0.09 s per query | Pass (gates 90%, 0.7, 100%) |
| Eval | Reranker (TEI `BAAI/bge-reranker-v2-m3`) | Not measured: the model download from huggingface.co failed |

## Defects fixed

| Defect | Fix |
|---|---|
| With `llm.enabled=false`, the MCP server still registered `ask`, because the ConfigMap always sets `LLM_BASE_URL`. `ask` then called a 35B model on CPU and timed out. | `templates/mcp.yaml` sets `LLM_BASE_URL=""` for the MCP server when `llm.enabled=false`. |
| `github_prs` read the public pull requests of every github.com URL in `GIT_REPOSITORIES`, also without a token. The source run failed on the rate limit, and `people` did not run. | Repositories come from `GIT_REPOSITORIES` only when `GITHUB_TOKEN` is set. `GITHUB_REPOSITORIES` still works without a token. |

## Notes for a laptop with antivirus TLS inspection

Avast Web/Mail Shield intercepts TLS and HTTP on `127.0.0.1`. `kubectl` then fails with
"certificate signed by unknown authority", and some local HTTP tests get an empty or broken body.
Run `kubectl` and `helm` in a container on the `kind` Docker network instead, with the kubeconfig
from `kind get kubeconfig --internal --name kg-test`:

```bash
docker run --rm --network kind -v "$PWD:/repo" -w /repo -e KUBECONFIG=/repo/kg-test-internal.kubeconfig \
  --entrypoint kubectl alpine/k8s:1.34.1 get pods -n kg
```

Not covered: real Jira, Confluence and GitHub data; Keycloak sign-in; the LLM (`ask`, summaries,
freshness); scale.
