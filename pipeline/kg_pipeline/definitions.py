"""Dagster code location for the knowledge graph pipeline.

Assets per repository: code_snapshot -> git_history, domain_model -> vector_index, summaries.
Source assets: jira_issues, confluence_pages -> source_vectors, doc_freshness.
Every asset applies the approved corrections (corrections.py) after its writes.

Environment (set by the Helm chart): GIT_REPOSITORIES, GIT_BRANCH, GIT_TOKEN, SOURCE_ARCHIVE_PATH,
NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, LLM_PROVIDER, LLM_BASE_URL, LLM_MODEL, EMBEDDINGS_URL,
EMBEDDINGS_MODEL, EMBEDDINGS_DIMENSIONS. Set DOMAIN_LLM=off to extract roles and entities only.
Summaries: SUMMARY_LLM=off (extractive only), SUMMARY_BUDGET. Freshness: FRESHNESS_LLM=on, FRESHNESS_LLM_BUDGET.
Jira: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECTS (comma-separated keys).
Confluence: CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN, CONFLUENCE_SPACES.
"""

import os
from contextlib import contextmanager
from pathlib import Path

import dagster as dg

from . import corrections, freshness, summaries
from .code.ingest import build_graph
from .domain.extract import extract
from .graph.neo4j_writer import Neo4jWriter
from .models import LLM, Embedder
from .sources import documents, git, history, jira
from .sources.atlassian import Atlassian
from .vectors import embed_repo, embed_sources

REPOS = {git.repo_name(u): u for u in os.environ.get("GIT_REPOSITORIES", "").split(",") if u}
BRANCH = os.environ.get("GIT_BRANCH", "main")
ARCHIVE = Path(os.environ.get("SOURCE_ARCHIVE_PATH", "/archive"))
JIRA_PROJECTS = [p.strip() for p in os.environ.get("JIRA_PROJECTS", "").split(",") if p.strip()]
CONFLUENCE_SPACES = [s.strip() for s in os.environ.get("CONFLUENCE_SPACES", "").split(",") if s.strip()]

repo_partitions = dg.StaticPartitionsDefinition(sorted(REPOS) or ["no-repositories-configured"])


@contextmanager
def _writer():
    """Neo4j writer of one asset. Approved corrections are applied again after the asset's writes."""
    with Neo4jWriter(os.environ["NEO4J_URI"], os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]) as w:
        yield w
        corrections.apply(w)


@dg.asset(partitions_def=repo_partitions, kinds={"neo4j"}, group_name="code")
def code_snapshot(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Code layer of one repository (files, symbols, imports, calls, chunks, ODM). Writes only changes."""
    name = context.partition_key
    if name not in REPOS:
        raise dg.Failure("Set GIT_REPOSITORIES (sources.git.repositories in the Helm values).")
    mirror_path = git.mirror(REPOS[name], ARCHIVE)
    sha = git.head(mirror_path, BRANCH)
    with git.checkout(mirror_path, sha) as work:
        graph = build_graph(work, name, scip_mode="auto", scip_cache=ARCHIVE / "cache" / "scip")
    with _writer() as writer:
        result = writer.write_delta(graph)
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
def git_history(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Commits since the last run, the files and symbols they changed, and the tickets they name."""
    name = context.partition_key
    with _writer() as w:
        stats = history.sync(w, git.mirror(REPOS[name], ARCHIVE), name)
    return dg.MaterializeResult(metadata={k: v for k, v in stats.items() if isinstance(v, int)})


@dg.asset(partitions_def=repo_partitions, deps=[code_snapshot], kinds={"neo4j"}, group_name="domain")
def domain_model(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Roles (entity, endpoint, ...) plus LLM-extracted concepts, business rules and capabilities."""
    llm = None if os.environ.get("DOMAIN_LLM") == "off" else LLM()
    with _writer() as w:
        stats = extract(w, context.partition_key, llm, ARCHIVE / "cache" / "llm")
    context.log.info("domain stats: %s", stats)
    return dg.MaterializeResult(metadata={k: v for k, v in stats.items() if isinstance(v, int)})


@dg.asset(partitions_def=repo_partitions, deps=[domain_model], kinds={"neo4j"}, group_name="domain")
def vector_index(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Embeddings for changed code chunks and domain concepts (Neo4j vector indexes)."""
    with _writer() as w:
        stats = embed_repo(w, context.partition_key, Embedder())
    return dg.MaterializeResult(metadata={"chunks": dg.MetadataValue.json(stats["chunks"]),
                                          "concepts": dg.MetadataValue.json(stats["concepts"])})


@dg.asset(name="summaries", partitions_def=repo_partitions, deps=[domain_model], kinds={"neo4j"}, group_name="knowledge")
def summaries_asset(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """File, module, repository and domain summaries; only changed inputs are summarised, within SUMMARY_BUDGET."""
    llm = None if os.environ.get("SUMMARY_LLM") == "off" else LLM()
    with _writer() as w:
        stats = summaries.summarize(w, context.partition_key, llm, Embedder(), cache=ARCHIVE / "cache" / "llm")
    return dg.MaterializeResult(metadata={k: v for k, v in stats.items() if isinstance(v, int)})


@dg.asset(kinds={"neo4j", "jira"}, group_name="sources")
def jira_issues(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Stories, bugs, epics and releases updated since the last run, for each project in JIRA_PROJECTS."""
    if not JIRA_PROJECTS:
        return dg.MaterializeResult(metadata={"skipped": "JIRA_PROJECTS is empty"})
    api, out = Atlassian(), {}
    with _writer() as w:
        for project in JIRA_PROJECTS:
            out[project] = jira.sync(w, api, project, acceptance_field=os.environ.get("JIRA_ACCEPTANCE_FIELD"))
    return dg.MaterializeResult(metadata={"projects": dg.MetadataValue.json(out)})


@dg.asset(kinds={"neo4j", "confluence"}, group_name="sources")
def confluence_pages(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Pages of each space in CONFLUENCE_SPACES; unchanged versions are skipped."""
    if not CONFLUENCE_SPACES:
        return dg.MaterializeResult(metadata={"skipped": "CONFLUENCE_SPACES is empty"})
    api, out = Atlassian(prefix="CONFLUENCE"), {}
    with _writer() as w:
        for space in CONFLUENCE_SPACES:
            out[space] = documents.sync_confluence(w, api, space)
    return dg.MaterializeResult(metadata={"spaces": dg.MetadataValue.json(out)})


@dg.asset(deps=[jira_issues, confluence_pages], kinds={"neo4j"}, group_name="sources")
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


CODE = [code_snapshot, git_history, domain_model, vector_index, summaries_asset]
SOURCES = [jira_issues, confluence_pages, source_vectors, doc_freshness]


# Incremental runs are cheap (only changes are written), so code runs every hour, sources every 15 minutes.
@dg.schedule(cron_schedule="0 * * * *", target=CODE, default_status=dg.DefaultScheduleStatus.STOPPED)
def hourly_code(context: dg.ScheduleEvaluationContext):
    for name in REPOS:
        yield dg.RunRequest(run_key=f"{name}:{context.scheduled_execution_time:%Y%m%d%H}", partition_key=name)


sources_every_15_min = dg.ScheduleDefinition(name="sources_every_15_min", target=SOURCES, cron_schedule="*/15 * * * *",
                                             default_status=dg.DefaultScheduleStatus.STOPPED)

from .trust_assets import TRUST, daily_reconcile  # noqa: E402  data trust: deletes, moves, duplicates

defs = dg.Definitions(assets=CODE + SOURCES + TRUST, schedules=[hourly_code, sources_every_15_min, daily_reconcile])
