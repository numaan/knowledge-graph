# Stream 1: data trust

This stream makes the graph match its sources and makes it safe to give to agents. It adds four
parts: reconciliation of deletes and moves, secret and PII masking, provenance on every source
node, and duplicate documents.

| Part | New files | Changed files |
|---|---|---|
| Deletes and moves | `pipeline/kg_pipeline/reconcile.py`, `pipeline/kg_pipeline/trust_assets.py` | `sources/jira.py`, `sources/documents.py`, `sources/history.py`, `sources/atlassian.py` (`NotFound`), `definitions.py` (one import, one line) |
| Masking | `pipeline/kg_pipeline/privacy.py` | `graph/neo4j_writer.py` (`upsert`), `vectors.py` (`_embed_label`), `models.py` (`LLM.json`), Helm `values.yaml` and `configmap-endpoints.yaml` |
| Provenance | `pipeline/kg_pipeline/provenance.py` | `sources/jira.py`, `sources/documents.py`, `sources/history.py`, `mcp/sources.go` |
| Duplicates | `pipeline/kg_pipeline/dedupe.py`, `mcp/trust.go` | `mcp/sources.go` (`search_knowledge`), `pyproject.toml` (`kg-dedupe`) |
| Tests | `pipeline/tests/test_privacy.py`, `pipeline/tests/test_trust.py`, `pipeline/tests/fixtures/trust/*.json` | `mcp/server_test.go` (`TestCollapseDuplicates`) |

## 1. Deletes and moves

The incremental syncs read only changed items, so they cannot see deletes. A reconciliation lists
only the ids in the source, compares them with the graph and removes the missing items, with their
sections and edges (`DETACH DELETE`).

```mermaid
flowchart LR
  L[List ids in the source] --> C{In the graph but not listed?}
  C -->|no| K[keep]
  C -->|yes| G[Get the item by id]
  G -->|404 / trashed| D[deleted: remove]
  G -->|archived| A[archived: remove]
  G -->|other key or space| M[moved: remove old node]
  G -->|same| K2[listed late: keep]
```

| Source | Listing | Missing item check | Result |
|---|---|---|---|
| Jira | `GET /rest/api/3/search/jql?jql=project="X"&fields=key&maxResults=5000` | `GET /rest/api/3/issue/{key}`: 404 means deleted; a different key means moved (Jira redirects the old key) | Old ticket node removed. Releases that are not in `/project/X/versions` are removed. |
| Confluence | `GET /wiki/api/v2/spaces/{id}/pages?status=current` (no bodies) | `GET /wiki/api/v2/pages/{id}`: 404, `trashed` or `deleted` means deleted; `archived`; another `spaceId` means moved | Document and its sections removed. The sync of the target space adds a moved page. |
| Document folder | Files on disk | A new path with the same content hash as a missing path is a move | A moved file keeps its nodes and embeddings (new id, path and url). Other missing files are removed. |
| Git history | `git rev-list <branch>` | A commit node that is not reachable from the branch was rewritten (rebase, force push) | Commit removed. A cursor on a removed commit is reset, so the next sync reads the last `--max` commits again. |

Other changes:

- `sync_confluence` now asks for `status=current`. The v2 API returns archived pages too by default.
- `sync_folder` detects moved files before it reads the folder, so a move is not a delete and a re-embed.

Safety: when a run would remove more than half of a project, space, folder or repository with more
than 20 items, it removes nothing and stops with `TooManyRemovals`. A broken listing or a lost
permission looks like a mass delete. Use `--force` (CLI) or `RECONCILE_FORCE=1` (Dagster) for one
run when the removal is correct. Each run stores its summary (first 100 ids per list) in the
`SyncState` node `reconcile:<source>:<key>`.

### Run it

```bash
kg-jira --project PREC --reconcile
kg-docs --confluence ENG --reconcile
kg-docs --path ./docs --name product-docs --reconcile
kg-git-history --repo /path/to/checkout --name my-repo --reconcile
```

Dagster: the asset `source_reconcile` (group `trust`) reconciles every project in `JIRA_PROJECTS`,
every space in `CONFLUENCE_SPACES` and every repository in `GIT_REPOSITORIES` (branch `GIT_BRANCH`).
The schedule `daily_reconcile` (03:00) runs `source_reconcile` and `document_duplicates`. It is
STOPPED by default. Start it in the Dagster UI.

## 2. Secret and PII masking

`kg_pipeline/privacy.py` finds two classes of values with regexes and an allow-list.

| Class | Kinds | Where it is replaced |
|---|---|---|
| Secret | `private_key`, `aws_access_key`, `github_token`, `gitlab_token`, `slack_token`, `slack_webhook`, `google_api_key`, `stripe_key`, `atlassian_token`, `anthropic_key`, `openai_key`, `npm_token`, `jwt`, `bearer_token`, `url_password` (`scheme://user:pass@host`), `password` (`password = "..."`, `api_key: ...` in config lines) | Everywhere. The stored `title`, `text` and `description` of every `:Source` node never contain the value. |
| PII | `email`, `phone`, `card` (Luhn check), `iban` (mod-97 check), `ssn` | `text_masked` on the node, embedding input, LLM prompts, MCP output. The stored `text` keeps PII for readers with the ACL. |

Placeholders keep the kind: `[SECRET:github_token]`, `[EMAIL]`, `[PHONE]`. The node property `pii`
lists the kinds found, for audits. It never holds a value.

| Apply point | Function | Effect |
|---|---|---|
| `Neo4jWriter.upsert` | `protect_row` | All sources (Jira, Confluence, folders, commits, releases) go through it. |
| `vectors._embed_label` | `mask` | Chunks, concepts, tickets and sections are masked before embedding. The embedding hash covers the masked text, so a mode change re-embeds. |
| `LLM.json` | `mask` | Every LLM prompt (domain model, requirement extraction and judge). |
| MCP `search_knowledge`, `get_context_for_ticket` | `coalesce(text_masked, text)` | Agents get masked text. |

The allow-list keeps example domains (`example.com`, `.test`, `localhost`), `noreply@` and `git@`
addresses, placeholders (`<token>`, `${VAR}`, `{{ x }}`, `changeme`, `xxx`), `os.environ[...]`
references and values that are already masked.

### Configuration

| Variable | Default | Effect |
|---|---|---|
| `KG_PII_MODE` | `mask` | `mask` or `off`. Any other value stops the run with an error. |
| `KG_PII_ALLOW` | empty | Extra allow-list, comma-separated: literal values or `re:<regex>`. |

Helm: `sources.privacy.mode` and `sources.privacy.allow` in `values.yaml` set both variables in the
`<name>-endpoints` ConfigMap.

## 3. Provenance

Every `:Source` node has `url`, `source_updated_at`, `ingested_at` (set by `upsert`) and `author`.

| Node | `url` | `source_updated_at` | `author` |
|---|---|---|---|
| Ticket | `<JIRA_BASE_URL>/browse/<key>` | `fields.updated` | reporter display name (creator if no reporter) |
| Release | `<JIRA_BASE_URL>/projects/<project>/versions/<id>` | `releaseDate` or `startDate` | none (the API has no author) |
| Commit | `<web remote>/commit/<sha>` (GitLab `/-/commit/`, Bitbucket `/commits/`) | author date | author name |
| Document, Section (Confluence) | page `webui` link | `version.createdAt` | `version.authorId` (Atlassian account id) |
| Document, Section (folder) | `--url-base` + path, else a `file://` URI | last git commit of the file, else file mtime | last git author, else none |

The commit link comes from `remote.origin.url` of the checkout or mirror, or from the `remote`
argument of `history.sync`. User names, tokens and ports are removed from the URL. Tickets also get
`jira_id` (the numeric issue id, which does not change when an issue moves).

MCP fields: `search_knowledge` returns `url`, `source_updated_at`, `ingested_at` and `author` (from
the document when the section has none). `get_context_for_ticket` returns them for the ticket, and
`url` for releases and commits. `get_change_history` returns the commit `url`.

## 4. Duplicate documents

`kg_pipeline/dedupe.py` (`kg-dedupe`, Dagster asset `document_duplicates`) finds copies across
Confluence spaces and document folders.

1. Each section gets `dup_hash` (hash of the text without case, Markdown markup and spacing) and
   `dup_sketch` (bottom-k MinHash, k = 64, of word 5-shingles).
2. The sketch of a document is the bottom-k of its section sketches. This is the sketch of the union
   of their shingles, so documents need no text.
3. Pairs with the same hash (`method: hash`, confidence 1.0) or an estimated Jaccard similarity of at
   least the threshold (`method: minhash`, confidence = similarity) are clustered.
4. Each other member of a cluster gets `DUPLICATE_OF` to one canonical node: Confluence before files,
   then the longer text, then the smaller id.

Incremental: `upsert` replaces node properties, so a rewritten section loses its sketch. Only new
and changed sections are read and hashed. Edges carry `content_hash` and only changed edges are
written or deleted.

| Setting | Default | Effect |
|---|---|---|
| `KG_DUP_THRESHOLD` or `--threshold` | 0.8 | Minimum estimated Jaccard similarity |
| `MIN_WORDS` | 20 | Shorter sections and documents are not compared |
| `MAX_POSTING` | 200 | A shingle in more items than this is boilerplate and does not propose pairs |

`search_knowledge` reads up to 3 × k candidates, applies the ACL filter, then keeps the best-ranked
row of each duplicate group. The other copies go into `also_in` (`id`, `title`, `url`). Only copies
that the server can see are named.

## 5. Test

```bash
cd pipeline && .venv/Scripts/python -m pytest -q      # 67 passed, 5 skipped without NEO4J_PASSWORD
cd mcp && go vet ./... && go test ./...
```

| Test | Covers |
|---|---|
| `test_privacy.py` | each secret kind, each PII kind, the allow-list and false positives (dates, versions, IP addresses, code), `KG_PII_ALLOW`, `KG_PII_MODE=off`, `protect_row`, LLM prompt masking |
| `test_trust.py` reconcile tests | Jira deleted, moved and late-listed issues and stale releases; the mass-delete guard; Confluence archived, deleted, trashed, moved and late-listed pages; folder moves and deletes; git rewritten history and cursor reset |
| `test_trust.py` provenance tests | web remote without credentials, commit links per host, ticket author and dates, file author from git and from mtime, secrets removed in `upsert` |
| `test_trust.py` duplicate tests | exact copy with other markup, near copy, no false pair, canonical choice, document sketch union, edge diff |
| `test_reconcile_and_duplicates_in_neo4j` | the Cypher against a real Neo4j (skipped without `NEO4J_PASSWORD`) |
| `TestCollapseDuplicates` (Go) | collapse order, `also_in`, the k limit |

The API fixtures are in `pipeline/tests/fixtures/trust/` and use the official response formats of
Jira Cloud REST v3 and Confluence Cloud REST v2.

## 6. Limits

- The Neo4j integration test did not run in this stream (Docker was stopped). The Cypher is
  checked only by the unit tests with a fake writer.
- Not tested against a real Jira, Confluence or GitHub. No credentials were available.
- Jira: a moved issue is removed under its old key. The new key comes in with the sync of its new
  project, if that project is configured. Commits that name the old key lose their `REFERENCES` edge.
- Jira and Confluence: at most 1000 missing items per run are checked one by one. The rest are
  treated as deleted.
- Requirements that a removed ticket or document specified stay in the graph until `kg-verify` runs again.
- Code chunk text is stored as it is in the repository. It is masked only for embeddings and LLM calls.
- The regexes find known token formats and `key = value` lines. They do not find random secrets
  without a known prefix or a key name (no entropy check).
- Phone numbers: a run of three digit groups with separators can be a false positive (for example a
  table of numbers). It is masked only in derived text, not in stored text.
- Duplicate clusters use transitive closure: A like B and B like C puts A and C in one cluster.
- The schedule `daily_reconcile` is daily. Duplicate edges are as fresh as its last run, or the last
  `kg-dedupe` run.
