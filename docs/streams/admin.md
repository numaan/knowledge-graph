# Admin: control API and admin UI

Status: implemented (control API, pipeline registry, web UI, Helm, Keycloak client `kg-web`). The `s3_documents`
asset runs sources of kind `s3` (job `s3_job`, CLI `kg-s3`). The control API
and the pipeline part were tested with fakes and SQLite; no run was made against a live Dagster daemon,
PostgreSQL or a real S3 server. Section 10 lists the differences from the first contract and the open risks.

## 1. Goal

An admin signs in to the web app, adds a data source, tests it, runs an ingestion pipeline and sees
the result. No redeploy. Four source kinds: git (codebase), s3 (object storage), confluence (wiki), jira.

```
Browser -> kg-web /admin -> /api/admin/* (proxy, adds token) -> kg-control (FastAPI)
                                                                   |-- PostgreSQL: sources, runs, audit
                                                                   |-- Dagster GraphQL: launch, status, cancel
Dagster run pods -> read sources + decrypt secrets from PostgreSQL at run time -> Neo4j
```

## 2. Decisions

| Decision | Choice | Reason |
|---|---|---|
| Language of the control API | Python (FastAPI) in the `kg-pipeline` image | Reuses `Atlassian`, `git` and `documents` code for connection tests. |
| Metadata store | PostgreSQL, database `knowledge` (already deployed) via SQLAlchemy Core | Tests use SQLite with the same code. |
| Secrets | Encrypted in PostgreSQL with Fernet. Key in `KG_SECRET_KEY` (Kubernetes Secret). Write-only: the API never returns a secret. | Admins can add a token in the UI without a redeploy. `env:NAME` stays a valid reference to an existing environment variable. |
| Trigger | Dagster GraphQL (`DagsterGraphQLClient`) | Runs keep the Dagster history, logs and run pods. |
| Schedules | One Dagster sensor `due_sources` reads `sources.schedule` (cron, UTC) each 60 s | Per-source schedules without a redeploy. |
| Auth | Keycloak access token (JWT, JWKS). Group `kg-admins` is required for every route except `/healthz`. | The realm and the group exist. Same pattern as `mcp/auth.go`. |
| Seed | `GIT_REPOSITORIES`, `JIRA_PROJECTS`, `CONFLUENCE_SPACES` stay valid. On start they seed rows with `origin = 'env'`. | Existing installs keep working. |
| Local mode | No `KG_OIDC_ISSUER`: no login, caller is `local`, treated as admin. | Same as the web app and MCP server. |

## 3. Data model (PostgreSQL)

`sources`

| Column | Type | Note |
|---|---|---|
| id | text PK | `src_` + 12 hex |
| kind | text | `git`, `s3`, `confluence`, `jira` |
| name | text unique | slug, `[a-z0-9][a-z0-9-]{0,62}`. For git it is the repository name (partition key). |
| config | json | Non-secret settings (section 5) |
| secret | text null | Fernet token of a JSON object. Never returned. |
| schedule | text null | 5-field cron, UTC. Null means manual only. |
| enabled | bool | A disabled source is skipped by the sensor and refuses `run`. |
| origin | text | `ui` or `env` |
| created_at, updated_at, created_by | | |

`runs`

| Column | Type | Note |
|---|---|---|
| id | text PK | `run_` + 12 hex |
| source_id | text FK null | Set null when the source is deleted. |
| source_name, kind | text | Copied at launch |
| status | text | `queued`, `running`, `succeeded`, `failed`, `canceled` |
| trigger | text | `manual`, `schedule` |
| mode | text | `incremental`, `full` |
| dagster_run_id | text null | |
| stats | json null | Asset metadata when the run ends |
| error | text null | |
| started_at, ended_at, requested_by | | |

Tables are created at control start and at seed time (`create_all`, idempotent). No migration tool exists.
`DATABASE_URL` has the form `postgresql+psycopg2://user:password@host:5432/knowledge`. The chart writes it to
the Secret `<name>-control`. Tests use SQLite.

`audit`: `id` (`aud_` + 12 hex), `at`, `actor`, `action`, `target`, `detail` (json). Actions: `source.create`, `source.update`,
`source.delete`, `source.test`, `run.start`, `run.cancel`. Detail never holds a secret value.

## 4. HTTP API

Base path `/api/v1`. JSON. `Authorization: Bearer <token>`. Errors: `{"error": "text"}` with status
400, 401, 403, 404, 409 or 502. The health route is `/api/v1/healthz` (the Helm probes use it). A list returns `{"items": [...]}`.

| Method and path | Body | Result |
|---|---|---|
| `GET /healthz` | | `{"ok": true}` (no auth), full path `/api/v1/healthz` |
| `GET /me` | | `{"user": "name", "is_admin": true}`. 200 for any valid token. |
| `GET /sources` | | `{"items": [Source]}` with `last_run` |
| `POST /sources` | `{kind, name, config, secret?, schedule?, enabled?}` | 201 `Source` |
| `GET /sources/{id}` | | `Source` |
| `PATCH /sources/{id}` | Any of `config, secret, schedule, enabled`. `secret: null` keeps it. `secret: {}` clears it. | `Source` |
| `DELETE /sources/{id}` | | 204. Graph data stays. |
| `POST /sources/{id}/test` | | `{ok, message, details}`. Checks the connection and the secret. Writes nothing to the graph. Always 200; writes audit `source.test`. |
| `POST /sources/{id}/run` | `{mode?: "incremental"\|"full"}` | 202 `Run`. 409 if a run of this source is active or the source is disabled. 502 when Dagster is unreachable (the `runs` row is stored as `failed`). |
| `GET /runs?source_id=&status=&limit=` | | `{"items": [Run]}` newest first. Default limit 50, max 200. |
| `GET /runs/{id}` | | `Run` with fresh status from Dagster and `log_tail` (list of strings, up to 200; stored secret values are masked) |
| `POST /runs/{id}/cancel` | | `Run` (status `canceled` at once). 409 if the run is final. 502 if Dagster fails. |
| `GET /audit?limit=` | | `{"items": [AuditEntry]}` |

`Source`: `{id, kind, name, enabled, schedule, config, has_secret, secret_keys, origin, last_run, created_at, updated_at}`.
`secret_keys` lists key names only (for example `["email","api_token"]`). `last_run` is a `Run` or null.
Timestamps are ISO 8601 UTC strings. `dagster_url` is `DAGSTER_WEBSERVER_URL/runs/<dagster_run_id>`, or null when
the variable is unset. `stats` is `{"<asset key>": {label: value}}` from the asset materializations, or null.
Contract fixtures (one JSON file per response) are in `web/tests/fixtures/control/`. The Python test
`pipeline/tests/test_control_contract.py` generates them (`KG_UPDATE_FIXTURES=1`) and the web tests read them.
`Run`: `{id, source_id, source_name, kind, status, trigger, mode, dagster_run_id, dagster_url, stats, error, started_at, ended_at, requested_by}`.

## 5. Source kinds

| Kind | `config` | `secret` keys | Pipeline asset |
|---|---|---|---|
| git | `url` (required), `branch` (default `main`) | `token` (optional) | `code_snapshot` -> `git_history` -> `domain_model` -> `vector_index` -> `summaries`, partition = `name` |
| jira | `base_url`, `projects: [KEY]` | `email`, `api_token` | `jira_issues` -> `source_vectors` |
| confluence | `base_url`, `spaces: [KEY]` | `email`, `api_token` | `confluence_pages` -> `source_vectors` |
| s3 | `endpoint` (optional), `bucket`, `prefix`, `region`, `include` (globs, optional) | `access_key_id`, `secret_access_key` | `s3_documents` -> `source_vectors` (job `s3_job`) |

S3 sources (asset `s3_documents`, code in `kg_pipeline/sources/s3.py`):

- `bucket` is required. `prefix` limits the keys. `include` is a list of globs on the key relative to the prefix
  (`fnmatchcase`: case sensitive, `*` also matches `/`). Only the suffixes of `kg-docs` are read (`.md`,
  `.markdown`, `.txt`, `.pdf`, `.docx`, `.pptx`, `.html`, `.htm`). Keys that end with `/` are ignored.
- Secret: `access_key_id` and `secret_access_key`. A ui-origin source without a secret is anonymous (unsigned
  requests; the `AWS_*` variables and the instance credentials are not read). An env-origin source without a
  secret keeps the default boto3 credential chain.
- A custom `endpoint` uses path-style addressing and the URL rules above. Redirects are refused.
- Objects over `KG_S3_MAX_BYTES` (default 52428800, 50 MiB) are not downloaded. They count as `skipped`.
- Change detection: an object with the same ETag, size and last-modified as the stored Document is not
  downloaded. A downloaded object with the same content hash only refreshes these three fields. Mode `full`
  downloads and rewrites every object.
- Each object is one `Document` with id `s3:<source name>:<key>`, `collection` = source name, `source` = `s3`, `url`
  `s3://<bucket>/<key>` and ACL group `docs:<source name>`. A user needs that group to see it.
- Reconcile removes the Documents of keys that are gone, only after a complete listing. Objects over the size
  limit count as existing and are never removed. The guard `TooManyRemovals` stops a run that would remove more
  than half of the Documents of a source (when more than 20); the asset has no force switch, use `kg-s3 --force`.
- Run stats (top-level, plain integers): `listed`, `new`, `changed`, `unchanged`, `skipped`, `removed`, `bytes`
  (downloaded bytes only), `failed`. Per-source results are in the `sources` JSON.
- A failing source (SSRF guard, listing error, `TooManyRemovals`) does not stop the other sources. The asset fails
  after the last source finished and the run shows each source error.

Validation: unknown keys are rejected. URLs must be `https://` (`http://` only when `KG_CONTROL_ALLOW_HTTP=1`).
The web form accepts `https://` only, always. `KG_CONTROL_ALLOW_HTTP=1` allows `http://` for direct API calls.
Hosts that resolve to loopback, link-local or metadata addresses are refused for `test` and for a run (SSRF guard)
unless `KG_CONTROL_ALLOW_PRIVATE=1`. `POST /sources/{id}/run` checks the stored git `url`, Jira/Confluence `base_url`
and S3 `endpoint` and answers 400 `SSRF guard: ...` (no run row, no launch). The `due_sources` sensor skips such a
source and logs a warning. Env-origin sources are checked the same way. A cluster-internal Git server needs
`KG_CONTROL_ALLOW_PRIVATE=1` on the control API and on the Dagster user code. In the Helm chart, set
`control.env.KG_CONTROL_ALLOW_PRIVATE: "1"` (and `KG_CONTROL_ALLOW_HTTP` if needed): the chart writes both values to
the ConfigMap `<name>-endpoints`, which the user code reads. Restart the user code pod after a change. The sensor
warning names the variable to set. A ui-origin git source never gets `GIT_TOKEN`, and git does not follow redirects
for it. DNS rebinding between the guard and the clone stays open for git.

## 6. Pipeline changes

- `kg_pipeline/sources_registry.py`: `get_sources(kind)` reads enabled sources at run time (rows, with env as seed),
  and `get_secret(source)` decrypts. Code never reads the registry at import time.
- `definitions.py`: git partitions use `DynamicPartitionsDefinition(name="repos")`. Jira and Confluence assets read
  projects, spaces and credentials from the registry. `Atlassian` accepts explicit credentials.
- Jobs: `git_job` (partitioned), `jira_job`, `confluence_job`, `s3_job`. A sensor `due_sources` (60 s) launches a run
  when a source cron is due and no run of it is active.
- Run result: there is no separate run-status sensor. The status refresh in `GET /runs/{id}` and `GET /runs`
  copies status and asset metadata to `runs`. Sensor `due_sources` does the same copy (`sync_runs`) at each tick
  for active rows, found by the tag `kg/run_id`, so scheduled runs end without an open UI. Mapping: `QUEUED`,
  `NOT_STARTED`, `STARTING` to `queued`; `STARTED` to `running`; `SUCCESS` to `succeeded`; `FAILURE` to
  `failed`; `CANCELED`, `CANCELING` to `canceled`.
- Sensor `registry_partitions` (30 s) seeds env rows and adds the dynamic partitions of git sources.
- Every launch carries the tags `kg/source`, `kg/source_id`, `kg/run_id` and `kg/mode`.
- Mode `full`: git runs skip the SCIP cache and read the full history. Jira and Confluence runs ignore the
  stored "updated since" cursor. Mode `full` does NOT bypass `write_delta` (it compares content hashes and
  writes only differences), the embedding cache or the summary cache. A full run re-reads the source, but it
  does not force a rewrite or a new LLM call for unchanged content.
- Jira and Confluence sources made in the UI are one per site, each with its own `projects` or `spaces`. Env
  seed makes one source `jira` and one source `confluence`. Only a row with `origin = 'env'` and no
  secret falls back to the env credentials (`ATLASSIAN_*`, `GIT_TOKEN`). A UI row without a secret runs without
  credentials, so an admin cannot send the platform tokens to a host of their choice.

## 7. Web UI (`web/`)

| Route | Content |
|---|---|
| `/admin` | Sources table: name, kind, status of last run, last run time, schedule, enabled. Buttons: Run, Test, Edit. Link "Add source". |
| `/admin/sources/new` | Form by kind. Secret fields are password inputs. |
| `/admin/sources/[id]` | Edit form (secret shows "set", with Replace and Clear). Test button with result. Recent runs of this source. Delete with confirmation. |
| `/admin/runs` | Runs table with filter by source and status. |
| `/admin/runs/[id]` | Status, timing, stats, error, log tail, link to Dagster. Cancel button while active. Polls every 3 s while active. |
| `/admin/audit` | Audit table |

The proxy route `/api/admin/[...path]` forwards to `CONTROL_API_URL` with the access token (read on the server
from the session cookie, never sent to the browser). It forwards only the paths in section 4 with the listed
methods, answers 404 for `..`, `%2E%2E`, `%2F` and backslash, and answers 413 for bodies over 1 MiB.

CSRF protection (one mechanism, two conditions, both required for POST, PATCH and DELETE; failure gives 403):
1. Same origin: the `Origin` header equals the request origin, or `Sec-Fetch-Site` is `same-origin`. The request
   origin is built from `X-Forwarded-Host` (first value) or `Host`, and from `X-Forwarded-Proto`. Without
   `X-Forwarded-Proto` the scheme of the `Origin` header is used and only host and port are compared.
2. The header `X-KG-Admin: 1`, sent by the admin client code (`web/src/lib/admin-client.ts`). The nav shows
"Admin" only when `/me` says `is_admin`. A non-admin who opens `/admin` sees a clear "admin only" message.

## 8. Deployment

`templates/control.yaml`: Deployment and Service `<name>-control` (port 8090), values `control.*`
(`control.enabled=false` by default; see the chart README). Env: `DATABASE_URL`, `KG_SECRET_KEY` (Secret),
`KG_OIDC_*`, `KG_CONTROL_ADMIN_GROUP`, `DAGSTER_GRAPHQL_HOST/PORT`, `DAGSTER_WEBSERVER_URL`. Dagster user code
gets `DATABASE_URL` and `KG_SECRET_KEY` too (optional references to the same Secret). The web Deployment gets
`CONTROL_API_URL=http://<name>-control:8090`. The command is `python -m kg_pipeline.control`. The console
script `kg-control` runs the same function.

- Dagster webserver: Service `<release>-dagster-webserver`, port 80 (the Dagster chart default). Override
  with `control.dagster.host` and `control.dagster.port`.
- Kubernetes Secret `<name>-control` holds `DATABASE_URL` and `KG_SECRET_KEY`. It has the annotation
  `helm.sh/resource-policy: keep`. The chart makes the key on first install and keeps it on upgrade.
- WARNING: if the key is lost or changed, every stored source secret is unreadable. The API then shows
  `secret_keys: []` and runs cannot decrypt. The admin must enter the secrets again. There is no rotation
  tool: to change the key, set the new key and enter all secrets again.
- Missing `KG_SECRET_KEY`: when `DATABASE_URL` is set and the key is missing or not a valid Fernet key, the
  control API refuses to start (exit with a message that names `KG_SECRET_KEY`). There is no 400-on-save path.
- Auth variables: `KG_OIDC_ISSUER` (empty = local mode), `KG_OIDC_AUDIENCE` (default `kg-mcp` when the
  issuer is set, as in the Go MCP server; an empty value stops the start),
  `KG_OIDC_GROUPS_CLAIM` (default `groups`), `KG_OIDC_JWKS_URL` (optional), `KG_CONTROL_ADMIN_GROUP`
  (default `kg-admins`).
- Keycloak: the dev realm has the confidential client `kg-web` with the default scope `kg-mcp`. That scope
  adds the audience `kg-mcp` and the `groups` claim. The chart default `control.oidc.audience=kg-mcp` matches.

## 9. Out of scope here

HA, GraphRAG communities, multi-tenant admin roles, key rotation tooling.

## 10. Deviations from the first contract and open risks

| Item | First contract | Built |
|---|---|---|
| Run status copy | Run-status sensor or refresh | Refresh in `GET /runs[/id]` plus `sync_runs` in sensor `due_sources`. No run-status sensor. |
| Missing `KG_SECRET_KEY` | Open | The API refuses to start when `DATABASE_URL` is set. |
| Health path | `/healthz` | `/api/v1/healthz` |
| `test` | Result | Always 200; `ok: false` names the guard or the HTTP error. Writes audit `source.test`. |
| Cancel | Dagster status | The row becomes `canceled` at once after Dagster terminate. |
| Dagster down on `run` | 502 | 502, and the `runs` row is stored as `failed` with an error. |
| Extra sensor | none | `registry_partitions` (30 s) adds dynamic partitions and seeds env rows. |
| `hourly_code`, `service_catalog` | n/a | `hourly_code` still loops over env repositories; UI-only repositories use `due_sources`. `service_catalog` uses env `GIT_BRANCH`. |
| URL scheme in UI | n/a | The web form accepts `https://` only. |
| Keycloak | Group and mapper | Client `kg-web` added; the existing scope `kg-mcp` carries audience and groups. |
| `s3_documents` | Later step | Built. `run` launches `s3_job`. Version 1.43.110 of boto3 is pinned in the extra `s3`. |
| S3 ACL | `KG_ACL_DEFAULT` | Documents get the group `docs:<source name>`, as `kg-docs --path` does. |

Open risks:

| Risk | Effect |
|---|---|
| No database uniqueness for one active run per source | Two parallel `POST /run` calls on PostgreSQL can both pass the check. |
| A scheduled `runs` row stays `queued` if Dagster never creates the run | No timeout. Cancel it in the UI. |
| The S3 `test` is not pinned against DNS rebinding | The SSRF guard resolves the host first; boto3 resolves it again. |
| The no-redirect hook of the S3 client is tested on a fake only | The `response-received` hook raises on 3xx; real botocore behaviour is not verified. |
| No S3 object versions, no bucket events | Only the current object is read; changes are found at the next run. |
| Reconcile needs a complete listing | A listing error ends the source run without a removal. |
| Mode `full` does not bypass caches | See section 6. |
| The log scrub does not mask a base64 of `email:token` for some lengths | No current path echoes it. |
| The pipeline part was not run against a live Dagster daemon | The first live run can show differences. |
| The root layout of the web app calls `/me` on each page render when `CONTROL_API_URL` is set | Up to 2 s extra when the control API is down. |
