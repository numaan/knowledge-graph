# knowledge-graph Helm chart

One-shot deployment of the knowledge graph platform for coding agents.

| Component | Image / chart | Toggle |
|---|---|---|
| Dagster (webserver, daemon, user code, K8s run launcher) | `dagster/dagster` chart 1.13.28 | `dagster.enabled` |
| PostgreSQL 17 + Apache AGE 1.7 + pgvector 0.8.7 | `postgres:17.11-bookworm` | always on |
| Neo4j Community + APOC | `neo4j:2026.10.1-community` | `graph.backend=neo4j` |
| Docling Serve | `quay.io/docling-project/docling-serve:v1.38.2` | `docling.enabled` |
| Embeddings (Qwen3-Embedding-0.6B) and reranker (bge-reranker-v2-m3) | `text-embeddings-inference:cpu-1.9.4` | `models.selfHosted`, `models.reranker.enabled` |
| Source archive PVC | — | always on |
| MCP server (your image) | `mcp.image` | `mcp.enabled` |

## Install

```bash
helm dependency build deploy/helm/knowledge-graph   # only if charts/ is empty
helm install kg deploy/helm/knowledge-graph -n kg --create-namespace --wait --timeout 20m
```

The first start is slow: PostgreSQL installs its extensions and the
embedding servers download their models.

### Small cluster

`values-small.yaml` fits 1 node with 8 vCPU / 32 GB, or 3 nodes with
4 vCPU / 16 GB each (for example k3s). It turns off the reranker, limits
Dagster to 2 runs at the same time and uses about 85 GiB of disk.

| | CPU request | RAM request | RAM limit |
|---|---|---|---|
| Services | 3.5 | 10.5 GiB | 18 GiB |
| 2 pipeline runs | 2 | 4 GiB | 12 GiB |
| Total | 5.5 | 14.5 GiB | 30 GiB |

```bash
helm install kg deploy/helm/knowledge-graph -n kg --create-namespace \
  -f deploy/helm/knowledge-graph/values-small.yaml --wait --timeout 20m
```

On a multi-node cluster with a node-local StorageClass (k3s `local-path`),
the archive PVC binds to one node, so the pipeline pods run on that node.

Typical overrides:

```bash
helm upgrade --install kg deploy/helm/knowledge-graph -n kg \
  --set graph.backend=age \
  --set sources.jira.baseUrl=https://your-org.atlassian.net \
  --set-string sources.credentials.ATLASSIAN_API_TOKEN=... \
  --set mcp.enabled=true --set mcp.image=registry/kg-mcp:0.1.0
```

Replace the Dagster example user code with your pipeline image under
`dagster.dagster-user-deployments.deployments[0]`.

## Notes

- Passwords are generated on first install and kept on upgrade. The
  `kg-credentials` Secret and the archive PVC survive `helm uninstall`.
- Neo4j Community has no RBAC. Keep it internal; the MCP server must apply
  Jira/Confluence permission filters.
- The default PostgreSQL setup runs `apt-get install` at start and needs
  egress to `apt.postgresql.org`. For production, build
  `images/postgres-age-pgvector/Dockerfile`, set `postgres.image` to it and set
  `postgres.extensions.install=false`.
- Use a ReadWriteMany StorageClass for `archive` when Dagster run pods can
  land on different nodes.
- Resource requests are sized for about 10 million relationships and are not
  measured. Benchmark and adjust.

## Admin control API (`control.*`)

Off by default. It serves the admin pages of the web UI (see `docs/streams/admin.md`). It runs the
`kg-pipeline` image and stores sources in the `knowledge` database. Set `web.enabled=true` too: the web
pod then gets `CONTROL_API_URL=http://<name>-control:8090`.

| Value | Default | Meaning |
|---|---|---|
| `control.enabled` | `false` | Render the control Deployment, Service and Secret. |
| `control.image` | `""` | The `kg-pipeline` image. Required when enabled. |
| `control.replicas` | `1` | Number of pods. |
| `control.port` | `8090` | Port of the container and the Service. |
| `control.secretKey.existingSecret` | `""` | Secret with the Fernet key. Empty: the chart makes `<name>-control` and keeps the key on upgrade. |
| `control.secretKey.key` | `KG_SECRET_KEY` | Key name in that Secret. |
| `control.oidc.issuer` | `""` | Keycloak realm URL. Empty: local mode without login. Keep the Service internal then. |
| `control.oidc.audience` | `""` | The `aud` claim of the token. Empty: not checked. Use `kg-mcp` with the dev realm. |
| `control.oidc.groupsClaim` | `groups` | Claim that holds the groups. |
| `control.oidc.adminGroup` | `""` | Sets `KG_CONTROL_ADMIN_GROUP`. Empty: the API uses `kg-admins`. |
| `control.dagster.host` | `""` | Empty: `<release>-dagster-webserver`. |
| `control.dagster.port` | `""` | Empty: `dagster.dagsterWebserver.service.port`, else `80`. |
| `control.dagster.webserverUrl` | `""` | Public Dagster UI URL for run links. Empty: no link. |
| `control.env` | `{}` | Extra env, for example `KG_CONTROL_ALLOW_HTTP: "1"` or `KG_CONTROL_ALLOW_PRIVATE: "1"`. Both are off by default. |
| `control.resources` | 50m CPU, 128Mi request; 512Mi limit | Pod resources. |

**Warning:** the Fernet key in the Secret `<name>-control` encrypts all source secrets. If you lose or
change it, the stored secrets cannot be read and you must enter them again. The Secret has
`helm.sh/resource-policy: keep`. With `existingSecret`, also change the `KG_SECRET_KEY` env of the Dagster
user deployment to read that Secret.
