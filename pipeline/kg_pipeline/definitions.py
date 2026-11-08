"""Dagster code location for the knowledge graph pipeline.

Assets per repository: code_snapshot -> git_history, domain_model -> vector_index, summaries.
Source assets: jira_issues, confluence_pages, s3_documents -> source_vectors, doc_freshness.
Every asset applies the approved corrections (corrections.py) after its writes.

Environment (set by the Helm chart): GIT_REPOSITORIES, GIT_BRANCH, GIT_TOKEN, SOURCE_ARCHIVE_PATH,
NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, LLM_PROVIDER, LLM_BASE_URL, LLM_MODEL, EMBEDDINGS_URL,
EMBEDDINGS_MODEL, EMBEDDINGS_DIMENSIONS. Set DOMAIN_LLM=off to extract roles and entities only.
Summaries: SUMMARY_LLM=off (extractive only), SUMMARY_BUDGET. Freshness: FRESHNESS_LLM=on, FRESHNESS_LLM_BUDGET.
Jira: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECTS (comma-separated keys).
Confluence: CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN, CONFLUENCE_SPACES.
S3 (MinIO, AWS, ...): sources in the registry only (bucket, prefix, include, endpoint; secret = access_key_id and
secret_access_key; none = anonymous); KG_S3_MAX_BYTES. Needs the `s3` extra (boto3).
GitHub, catalog and people: see people_assets.py (github_prs, service_catalog, people).
"""

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import dagster as dg

from . import corrections, due_sources as due, freshness, sources_registry, summaries
from .code import links
from .code.ingest import build_graph
from .control import probe
from .domain.extract import extract
from .graph.neo4j_writer import Neo4jWriter
from .models import LLM, Embedder
from .people_assets import github_prs, people_asset, service_catalog_asset
from .sources import documents, git, history, jira, permissions, s3
from .sources.atlassian import Atlassian
from .telemetry import traced_asset
from .vectors import embed_repo, embed_sources

REPOS = {git.repo_name(u): u for u in os.environ.get("GIT_REPOSITORIES", "").split(",") if u}
BRANCH = os.environ.get("GIT_BRANCH", "main")
ARCHIVE = Path(os.environ.get("SOURCE_ARCHIVE_PATH", "/archive"))
JIRA_PROJECTS = [p.strip() for p in os.environ.get("JIRA_PROJECTS", "").split(",") if p.strip()]
CONFLUENCE_SPACES = [s.strip() for s in os.environ.get("CONFLUENCE_SPACES", "").split(",") if s.strip()]

# Repositories are dynamic partitions: the control API adds a key when a git source is created and removes it on
# delete; `sync_partitions` (sensor below) registers the env and seeded repositories. The name is fixed by the control
# API (control/dagster_client.py PARTITIONS_DEF and GIT_JOB); it is repeated here so that this module does not import
# the control package (fastapi extra) at import time.
repo_partitions = dg.DynamicPartitionsDefinition(name="repos")
GIT_JOB = "git_job"
JIRA_JOB = "jira_job"
CONFLUENCE_JOB = "confluence_job"
S3_JOB = "s3_job"
MODE_TAG = "kg/mode"
SOURCE_TAG = "kg/source"


def _full(context: dg.AssetExecutionContext) -> bool:
    """True when the run was started with mode full (tag kg/mode, set by the control API)."""
    return context.run_tags.get(MODE_TAG) == "full"


def _repo(name: str) -> tuple[str, str, str | None]:
    """URL, branch and token of the git source with this name, read from the registry at run time."""
    return _source(name)[:3]


def _source(name: str) -> tuple[str, str, str | None, str]:
    """Like _repo, with the origin of the source as fourth item."""
    for src in sources_registry.get_sources("git"):
        if src.name == name:
            branch = src.config.get("branch") or os.environ.get("GIT_BRANCH", "main")
            token = sources_registry.get_secret(src).get("token")
            if not token and src.origin == "env":  # a UI source without a secret runs without credentials
                token = os.environ.get("GIT_TOKEN") or None
            return src.config["url"], branch, token, src.origin
    raise dg.Failure("Set GIT_REPOSITORIES (sources.git.repositories in the Helm values).")


class _RegistryRepos:
    """Mapping name -> URL over the registry, for assets that still take a dict (service_catalog)."""

    def __getitem__(self, name: str) -> str:
        return _repo(name)[0]

    def mirror_kwargs(self, name: str) -> dict:
        """Credential for git.mirror. _repo already applied the env rule, so git must not add GIT_TOKEN itself."""
        _, _, token, origin = _source(name)
        return {"token": token, "env_fallback": False, "restricted": origin != "env"}


@contextmanager
def _writer():
    """Neo4j writer of one asset. Approved corrections are applied again after the asset's writes."""
    with Neo4jWriter(os.environ["NEO4J_URI"], os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]) as w:
        yield w
        corrections.apply(w)


@dg.asset(partitions_def=repo_partitions, kinds={"neo4j"}, group_name="code")
@traced_asset
def code_snapshot(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Code layer of one repository (files, symbols, imports, calls, chunks, ODM). Writes only changes."""
    name = context.partition_key
    url, branch, token, origin = _source(name)
    mirror_path = git.mirror(url, ARCHIVE, token=token, env_fallback=False, restricted=origin != "env")
    sha = git.head(mirror_path, branch)
    # Mode full: no SCIP cache, so every folder is indexed again. write_delta compares content hashes and still
    # writes only the differences.
    scip_cache = None if _full(context) else ARCHIVE / "cache" / "scip"
    with git.checkout(mirror_path, sha) as work:
        graph = build_graph(work, name, scip_mode="auto", scip_cache=scip_cache)
    with _writer() as writer:
        result = writer.write_delta(graph)
        context.log.info("links: %s", links.sync(writer, links.load_config(None)))  # HTTP calls and topics across repos
    stats = graph.stats()
    context.log.info("graph stats: %s", stats)
    return dg.MaterializeResult(metadata={
        "commit": sha, "nodes": result["nodes"], "edges": result["edges"],
        "nodes_written": result["nodes_written"], "edges_written": result["edges_written"],
        "stale_nodes_deleted": result["stale_nodes_deleted"],
        "files_parsed": stats["counters"].get("files_parsed", 0),
        "files_scip_covered": stats["counters"].get("files_scip_covered", 0),
        "files_parse_failed": stats["counters"].get("files_parse_failed", 0),
        "edges_by_type": dg.MetadataValue.json(stats["edges"]),
    })


@dg.asset(partitions_def=repo_partitions, deps=[code_snapshot], kinds={"neo4j"}, group_name="code")
@traced_asset
def git_history(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Commits since the last run, the files and symbols they changed, and the tickets they name."""
    name = context.partition_key
    url, _, token, origin = _source(name)
    with _writer() as w:
        mirror_path = git.mirror(url, ARCHIVE, token=token, env_fallback=False, restricted=origin != "env")
        stats = history.sync(w, mirror_path, name, full=_full(context))
    return dg.MaterializeResult(metadata={k: v for k, v in stats.items() if isinstance(v, int)})


@dg.asset(partitions_def=repo_partitions, deps=[code_snapshot], kinds={"neo4j"}, group_name="domain")
@traced_asset
def domain_model(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Roles (entity, endpoint, ...) plus LLM-extracted concepts, business rules and capabilities."""
    _repo(context.partition_key)  # unknown partition: same failure as code_snapshot
    llm = None if os.environ.get("DOMAIN_LLM") == "off" else LLM()
    with _writer() as w:
        stats = extract(w, context.partition_key, llm, ARCHIVE / "cache" / "llm")
    context.log.info("domain stats: %s", stats)
    return dg.MaterializeResult(metadata={k: v for k, v in stats.items() if isinstance(v, int)})


@dg.asset(partitions_def=repo_partitions, deps=[domain_model], kinds={"neo4j"}, group_name="domain")
@traced_asset
def vector_index(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Embeddings for changed code chunks and domain concepts (Neo4j vector indexes)."""
    _repo(context.partition_key)
    with _writer() as w:
        stats = embed_repo(w, context.partition_key, Embedder())
    return dg.MaterializeResult(metadata={"chunks": dg.MetadataValue.json(stats["chunks"]),
                                          "concepts": dg.MetadataValue.json(stats["concepts"])})


@dg.asset(name="summaries", partitions_def=repo_partitions, deps=[domain_model], kinds={"neo4j"}, group_name="knowledge")
def summaries_asset(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """File, module, repository and domain summaries; only changed inputs are summarised, within SUMMARY_BUDGET."""
    _repo(context.partition_key)
    llm = None if os.environ.get("SUMMARY_LLM") == "off" else LLM()
    with _writer() as w:
        stats = summaries.summarize(w, context.partition_key, llm, Embedder(), cache=ARCHIVE / "cache" / "llm")
    return dg.MaterializeResult(metadata={k: v for k, v in stats.items() if isinstance(v, int)})


def _atlassian(src: sources_registry.Source, prefix: str) -> Atlassian:
    """Client with the credentials of a registry source. Missing values of an env source fall back to the env (prefix_*)."""
    secret = sources_registry.get_secret(src)
    return Atlassian(src.config.get("base_url") or None, secret.get("email"), secret.get("api_token"), prefix=prefix,
                     use_env=src.origin == "env")


def _targets(kind: str, key: str):
    """(source, item) pairs: every project (jira) or space (confluence) of every enabled source, read at run time."""
    return [(s, item) for s in sources_registry.get_sources(kind) for item in s.config.get(key) or []]


def _label(src: sources_registry.Source, item: str) -> str:
    return item if src.name in (sources_registry.JIRA_NAME, sources_registry.CONFLUENCE_NAME) else f"{src.name}:{item}"


@dg.asset(kinds={"neo4j", "jira"}, group_name="sources")
@traced_asset
def jira_issues(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Stories, bugs, epics and releases updated since the last run, for each project of each enabled Jira source."""
    targets = _targets("jira", "projects")
    if not targets:
        return dg.MaterializeResult(metadata={"skipped": "JIRA_PROJECTS is empty"})
    apis, out = {}, {}
    with _writer() as w:
        for src, project in targets:
            api = apis.get(src.name) or apis.setdefault(src.name, _atlassian(src, "JIRA"))
            out[_label(src, project)] = jira.sync(w, api, project, full=_full(context),
                                                  acceptance_field=os.environ.get("JIRA_ACCEPTANCE_FIELD"))
    return dg.MaterializeResult(metadata={"projects": dg.MetadataValue.json(out)})


@dg.asset(kinds={"neo4j", "confluence"}, group_name="sources")
@traced_asset
def confluence_pages(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Pages of each space of each enabled Confluence source; unchanged versions are skipped (not in mode full)."""
    targets = _targets("confluence", "spaces")
    if not targets:
        return dg.MaterializeResult(metadata={"skipped": "CONFLUENCE_SPACES is empty"})
    apis, out = {}, {}
    with _writer() as w:
        for src, space in targets:
            api = apis.get(src.name) or apis.setdefault(src.name, _atlassian(src, "CONFLUENCE"))
            out[_label(src, space)] = documents.sync_confluence(w, api, space, full=_full(context))
    return dg.MaterializeResult(metadata={"spaces": dg.MetadataValue.json(out)})


@dg.asset(kinds={"neo4j", "s3"}, group_name="sources")
@traced_asset
def s3_documents(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Documents of each enabled S3 source (bucket + prefix): new and changed objects, and removal of deleted keys."""
    sources = sources_registry.get_sources("s3")
    # A run of the control API (POST /run, due_sources sensor) carries the tag kg/source: it handles that source only.
    # The schedule and a manual run have no tag: they handle every enabled source and never force.
    only = context.run_tags.get(SOURCE_TAG)
    if only:
        sources = [s for s in sources if s.name == only]
        if not sources:
            raise dg.Failure(f"s3 source '{only}' of this run was not found or is disabled")
    if not sources:
        return dg.MaterializeResult(metadata={"skipped": "no enabled s3 source"})
    out, errors, total = {}, {}, s3.new_stats()
    force = bool(only) and _full(context)
    with _writer() as w:
        for src in sources:
            try:
                probe.guard_source("s3", src.config)   # SSRF guard, as in POST /run
                # A ui source never uses the platform credentials: without a secret the client is anonymous.
                secret, ui = sources_registry.get_secret(src), src.origin != "env"
                client = s3.build_client(src.config, secret, ui_origin=ui)
                # Mode full of ONE named source is an explicit admin action (the UI asks for a confirmation):
                # it lifts the removal guard of that source only.
                out[src.name] = s3.call_with_region_hint(
                    src.config, secret, ui,
                    lambda c: s3.sync(w, c, src.name, src.config, full=_full(context), force=force), client)
            except Exception as e:   # the other sources still run; the asset fails at the end
                context.log.error("s3 source %s failed: %s: %s", src.name, type(e).__name__, e)
                errors[src.name] = f"{type(e).__name__}: {e}"
    for stats in out.values():
        for k in total:
            total[k] += stats.get(k, 0)
    meta = {**total, "failed": sum(s.get("failed", 0) for s in out.values()), "sources": dg.MetadataValue.json(out)}
    if errors:
        raise dg.Failure(f"s3 sources failed: {'; '.join(f'{n}: {m}' for n, m in errors.items())}", metadata=meta)
    return dg.MaterializeResult(metadata=meta)


@dg.asset(deps=[jira_issues, confluence_pages, github_prs, s3_documents], kinds={"neo4j"}, group_name="sources")
@traced_asset
def source_vectors(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Embeddings for changed tickets and document sections."""
    with _writer() as w:
        stats = embed_sources(w, Embedder())
    return dg.MaterializeResult(metadata={"sources": dg.MetadataValue.json(stats["sources"])})


@dg.asset(deps=[confluence_pages], kinds={"neo4j"}, group_name="knowledge")
def doc_freshness(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Stale flags on documents whose code changed later; FRESHNESS_LLM=on also checks for conflicts."""
    llm = LLM() if os.environ.get("FRESHNESS_LLM") == "on" else None
    with _writer() as w:
        stats = freshness.run(w, llm, cache=ARCHIVE / "cache" / "llm")
    return dg.MaterializeResult(metadata={k: v for k, v in stats.items() if isinstance(v, int)})


service_catalog = service_catalog_asset(repo_partitions, code_snapshot, _RegistryRepos(), BRANCH, ARCHIVE)
people = people_asset([git_history, jira_issues, confluence_pages, github_prs, service_catalog])


@dg.asset(deps=[jira_issues, confluence_pages], kinds={"neo4j", "jira", "confluence"}, group_name="sources")
def source_permissions(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Precise ACLs from Jira permissions and security levels and Confluence restrictions (KG_ACL_DEFAULT)."""
    out = {}
    with _writer() as w:
        jira_targets = _targets("jira", "projects")
        if jira_targets:
            apis = {}
            out["jira"] = [permissions.sync_jira(w, apis.get(s.name) or apis.setdefault(s.name, _atlassian(s, "JIRA")), p)
                           for s, p in jira_targets]
        conf_targets = _targets("confluence", "spaces")
        if conf_targets:
            apis = {}
            out["confluence"] = [
                permissions.sync_confluence(w, apis.get(s.name) or apis.setdefault(s.name, _atlassian(s, "CONFLUENCE")), sp)
                for s, sp in conf_targets]
    return dg.MaterializeResult(metadata={"permissions": dg.MetadataValue.json(out)})


CODE = [code_snapshot, git_history, domain_model, vector_index, summaries_asset, service_catalog]
SOURCES = [jira_issues, confluence_pages, s3_documents, source_permissions, github_prs, source_vectors, doc_freshness, people]


# Incremental runs are cheap (only changes are written), so code runs every hour, sources every 15 minutes.
@dg.schedule(cron_schedule="0 * * * *", target=CODE, default_status=dg.DefaultScheduleStatus.STOPPED)
def hourly_code(context: dg.ScheduleEvaluationContext):
    for name in REPOS:
        yield dg.RunRequest(run_key=f"{name}:{context.scheduled_execution_time:%Y%m%d%H}", partition_key=name)


sources_every_15_min = dg.ScheduleDefinition(name="sources_every_15_min", target=SOURCES, cron_schedule="*/15 * * * *",
                                             default_status=dg.DefaultScheduleStatus.STOPPED)

git_job = dg.define_asset_job(
    GIT_JOB, selection=[code_snapshot, git_history, domain_model, vector_index, summaries_asset])
jira_job = dg.define_asset_job(JIRA_JOB, selection=[jira_issues, source_vectors])
confluence_job = dg.define_asset_job(CONFLUENCE_JOB, selection=[confluence_pages, source_vectors])
s3_job = dg.define_asset_job(S3_JOB, selection=[s3_documents, source_vectors])


def sync_partitions(instance: dg.DagsterInstance) -> list[str]:
    """Seed the env sources into the registry (database mode) and add a partition for every git source.

    Returns the keys added. Idempotent. This is a function that a sensor calls, not module code: the
    registry reads the database, and the module must import without a database (tests, `dagster dev` without
    DATABASE_URL). A sensor is the Dagster hook that runs in the user-code process after start and needs no run.
    """
    if os.environ.get("DATABASE_URL", "").strip():
        sources_registry.seed_from_env()
    have = set(instance.get_dynamic_partitions(repo_partitions.name))
    added = sorted({s.name for s in sources_registry.get_sources("git")} - have)
    if added:
        instance.add_dynamic_partitions(repo_partitions.name, added)
    return added


@dg.sensor(name="registry_partitions", minimum_interval_seconds=30, default_status=dg.DefaultSensorStatus.RUNNING)
def registry_partitions(context: dg.SensorEvaluationContext):
    added = sync_partitions(context.instance)
    return dg.SkipReason(f"partitions added: {', '.join(added)}" if added else "partitions are up to date")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dg.sensor(name="due_sources", minimum_interval_seconds=60, default_status=dg.DefaultSensorStatus.RUNNING,
           jobs=[git_job, jira_job, confluence_job, s3_job])
def due_sources(context: dg.SensorEvaluationContext):
    """Launch the job of each enabled source whose cron schedule (UTC) is due, as the control API would."""
    if not os.environ.get("DATABASE_URL", "").strip():
        return dg.SkipReason("DATABASE_URL is not set: no registry schedules")
    due.sync_runs(context.instance)
    return due.due_requests(_now()) or dg.SkipReason("no source is due")


from .trust_assets import TRUST, daily_reconcile  # noqa: E402  data trust: deletes, moves, duplicates

defs = dg.Definitions(assets=CODE + SOURCES + TRUST, schedules=[hourly_code, sources_every_15_min, daily_reconcile],
                      jobs=[git_job, jira_job, confluence_job, s3_job], sensors=[registry_partitions, due_sources])
