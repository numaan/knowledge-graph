"""Dagster assets for data trust: reconciliation of deletes and moves, and duplicate documents.

The schedule is STOPPED by default; start it in the Dagster UI. Reconciliation lists only ids, so a
daily run is cheap; it removes what the incremental syncs cannot see (deletes, moves, archives,
rewritten history). Set RECONCILE_FORCE=1 for one run to allow a removal of more than half a source.
"""
import os
from pathlib import Path

import dagster as dg

from . import dedupe, reconcile
from .graph.neo4j_writer import Neo4jWriter
from .sources import git
from .sources.atlassian import Atlassian

_csv = lambda name: [s.strip() for s in os.environ.get(name, "").split(",") if s.strip()]


def _writer() -> Neo4jWriter:
    return Neo4jWriter(os.environ["NEO4J_URI"], os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"])


@dg.asset(kinds={"neo4j"}, group_name="trust", deps=["jira_issues", "confluence_pages", "git_history"])
def source_reconcile(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Remove deleted and moved Jira issues, deleted, archived and moved Confluence pages, and commits of rewritten history."""
    force = os.environ.get("RECONCILE_FORCE") == "1"
    archive = Path(os.environ.get("SOURCE_ARCHIVE_PATH", "/archive"))
    out: dict[str, dict] = {}
    with _writer() as w:
        if projects := _csv("JIRA_PROJECTS"):
            api = Atlassian()
            out |= {f"jira:{p}": reconcile.jira(w, api, p, force) for p in projects}
        if spaces := _csv("CONFLUENCE_SPACES"):
            api = Atlassian(prefix="CONFLUENCE")
            out |= {f"confluence:{s}": reconcile.confluence(w, api, s, force) for s in spaces}
        for url in _csv("GIT_REPOSITORIES"):
            name = git.repo_name(url)
            out[f"git:{name}"] = reconcile.git_history(w, git.mirror(url, archive), name,
                                                       os.environ.get("GIT_BRANCH", "main"), force)
    context.log.info("reconcile: %s", out)
    return dg.MaterializeResult(metadata={"sources": dg.MetadataValue.json(out),
                                          "removed": sum(v.get("removed", 0) for v in out.values())})


@dg.asset(kinds={"neo4j"}, group_name="trust", deps=[source_reconcile, "confluence_pages"])
def document_duplicates(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """DUPLICATE_OF edges between near-identical documents and sections (search_knowledge collapses them)."""
    with _writer() as w:
        stats = dedupe.detect(w)
    return dg.MaterializeResult(metadata=stats)


TRUST = [source_reconcile, document_duplicates]
daily_reconcile = dg.ScheduleDefinition(name="daily_reconcile", target=TRUST, cron_schedule="0 3 * * *",
                                        default_status=dg.DefaultScheduleStatus.STOPPED)
