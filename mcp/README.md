# kg-mcp

Read-only MCP server over the knowledge graph that `pipeline/` builds in Neo4j. One static Go
binary (~14 MB, ~20 MB image). Coding agents use it to search code by meaning, see the domain
model, check the impact of a change, find tests and trace requirements to code.

```
agent ──MCP──> kg-mcp ──bolt (read tx, 15 s, 500 rows)──> Neo4j
                  └──── /v1/embeddings (query vector) ───> Ollama or TEI
```

## Tools

| Tool | Use it to | Input |
|---|---|---|
| `search_graph` | Find code for a question. Vector + full-text + concept search, RRF, then graph expansion (concepts, rules, callers, callees, tests, code). | `query`, `repo`, `k` |
| `find_symbol` | Look up classes, functions and methods by name. `Invit*` works. | `name`, `repo`, `kind`, `limit` |
| `get_symbol_code` | Read the full source of one symbol. | `symbol` (id or qualified name) |
| `impact_analysis` | See what can break: callers, subtypes, tests, endpoints, requirements, concepts. | `symbol`, `depth` (1-4) |
| `find_related_tests` | Know which tests to run after a change. | `symbol` |
| `get_neighbors` | Walk typed edges from any node. | `node_id`, `edge_types`, `direction`, `depth` |
| `trace_requirement` | See the verdict and evidence of a requirement (from `kg-verify`). | id, ref (`FR-P10-03-02`) or text |
| `domain_overview` | Business view: capabilities, concepts, rules, entities, endpoints. No repo: list repositories. | `repo` |
| `get_context_for_ticket` | Start work on a Jira ticket: text, epic, links, release, commits, code, requirements. | `key` |
| `search_knowledge` | Find tickets and document or wiki sections. | `query`, `kind`, `k` |
| `get_change_history` | See who changed a symbol or file, when and why. | `symbol`, `limit` |
| `read_cypher` | Anything else, in read-only Cypher. Off when `KG_ACL_GROUPS` is not `*`. | `query`, `params` |

A name that matches more than one symbol returns an error with the candidate ids, so the agent can
call again with one id.

## Safety

- Every query runs in a Neo4j **read** transaction, so the server cannot write.
- `read_cypher` also rejects write clauses, `LOAD CSV` and all procedures except the index, label
  and schema procedures. Edge-type filters must match `^[A-Z][A-Z0-9_]*$`.
- Results never include `embedding` vectors.
- Use a Neo4j user with the `reader` role in production (Enterprise), or keep the server inside the
  cluster.

## Configuration

| Variable | Default |
|---|---|
| `NEO4J_URI` | `bolt://localhost:7687` |
| `NEO4J_USER` / `NEO4J_PASSWORD` | `neo4j` / (none) |
| `EMBEDDINGS_URL` | `http://localhost:8080` (TEI). Ollama: `http://localhost:11434` |
| `EMBEDDINGS_MODEL` | `Qwen/Qwen3-Embedding-0.6B`. Ollama: `qwen3-embedding:0.6b` |
| `EMBEDDINGS_API_KEY` | (none) |
| `KG_DEFAULT_REPO` | (none: search all repositories) |
| `KG_ACL_GROUPS` | `*`. Or e.g. `repo:app,jira:APP,confluence:ENG`: the server sees only these groups |
| `MCP_HTTP_ADDR` or `-http` | (none: stdio) |

Use the same embedding model as the pipeline, or the vector search returns nonsense.

## Build and test

```bash
cd mcp
go build -o kg-mcp.exe .
go test ./...                     # unit tests; the integration test needs NEO4J_PASSWORD
docker build -t kg-mcp:0.1.0 .
```

## Register

Claude Code, local stdio (the password comes from your shell environment):

```bash
claude mcp add kg --scope user -e NEO4J_PASSWORD=${NEO4J_PASSWORD} -e EMBEDDINGS_URL=http://localhost:11434 -e EMBEDDINGS_MODEL=qwen3-embedding:0.6b -e KG_DEFAULT_REPO=my-repo -- /path/to/kg-mcp
```

Project file: copy `.mcp.json.example` to the root of the repo that the agent works on, as
`.mcp.json`. Cursor, Codex and opencode take the same command and env.

Cluster: set `mcp.enabled=true` and `mcp.image` in the Helm chart. The pod serves `/mcp`
(Streamable HTTP) and `/healthz`. Port-forward and register it as an `http` server:

```bash
kubectl port-forward svc/kg-mcp 8000:8000
claude mcp add kg --transport http http://localhost:8000/mcp
```

## Access control

`KG_ACL_GROUPS` comes from the environment, not from a tool argument, so an agent cannot widen it.
Every query gets the server's repositories as `$repos`, and source nodes are filtered by their `acl`.
Run one server per audience.
