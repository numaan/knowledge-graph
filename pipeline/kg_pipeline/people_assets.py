"""Dagster assets of the people stream: GitHub pull requests, service catalog and people.

github_prs (every repository in GITHUB_REPOSITORIES, or the github.com URLs of GIT_REPOSITORIES),
service_catalog (per repository partition, after code_snapshot) and people (after both and the other
sources). Env: GITHUB_TOKEN, GITHUB_API_URL, GITHUB_REPOSITORIES ("owner/repo=name,..."),
PEOPLE_ALIASES (path of people-aliases.yaml).
"""

import os
from pathlib import Path

import dagster as dg

from . import people as people_mod
from .graph.neo4j_writer import Neo4jWriter
from .sources import catalog, git, github

GIT_URLS = [u for u in os.environ.get("GIT_REPOSITORIES", "").split(",") if u]
GITHUB_REPOS = github.repositories(os.environ.get("GITHUB_REPOSITORIES", ""), GIT_URLS)


def _writer() -> Neo4jWriter:
    return Neo4jWriter(os.environ["NEO4J_URI"], os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"])


@dg.asset(kinds={"neo4j", "github"}, group_name="sources")
def github_prs(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """Pull requests, reviews and review comments updated since the last run, per GitHub repository."""
    if not GITHUB_REPOS:
        return dg.MaterializeResult(metadata={"skipped": "no GitHub repositories configured"})
    api, out = github.GitHub(), {}
    with _writer() as w:
        for slug, name in GITHUB_REPOS.items():
            out[slug] = github.sync(w, github.GitHubForge(api, slug), slug, name)
    return dg.MaterializeResult(metadata={"repositories": dg.MetadataValue.json(out), "api_calls": api.calls})


def service_catalog_asset(partitions_def, code_snapshot, repos: dict, branch: str, archive: Path):
    @dg.asset(name="service_catalog", partitions_def=partitions_def, deps=[code_snapshot], kinds={"neo4j"},
              group_name="code")
    def service_catalog(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
        """Backstage catalog-info.yaml entities and CODEOWNERS of one repository."""
        name = context.partition_key
        mirror_path = git.mirror(repos[name], archive)
        with git.checkout(mirror_path, git.head(mirror_path, branch)) as work, _writer() as w:
            stats = catalog.sync(w, work, name)
        return dg.MaterializeResult(metadata={k: v for k, v in stats.items() if isinstance(v, (int, bool))})
    return service_catalog


def people_asset(deps: list):
    @dg.asset(name="people", deps=deps, kinds={"neo4j"}, group_name="sources")
    def people(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
        """:Person nodes merged from git, Jira, Confluence, GitHub and the catalog; writes only changes."""
        with _writer() as w:
            stats = people_mod.sync(w, os.environ.get("PEOPLE_ALIASES"))
        return dg.MaterializeResult(metadata=stats)
    return people
