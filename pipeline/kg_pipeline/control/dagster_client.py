"""Thin interface over `dagster_graphql.DagsterGraphQLClient` (Dagster 1.13).

Verified against the installed package source:
- launch: `submit_job_execution(job_name, run_config=, tags=)`; tags go to `executionMetadata.tags`.
  A partitioned run carries the tag `dagster/partition` (dagster._core.storage.tags.PARTITION_NAME_TAG).
- status: `get_run_status(run_id)` returns a `DagsterRunStatus` enum.
- terminate: `terminate_run(run_id)`.
- partitions and logs have no public method, so they use `_execute` with the mutation
  `addDynamicPartition` / `deleteDynamicPartitions` (args `repositorySelector`, `partitionsDefName`,
  `partitionKey(s)`) and the query `logsForRun` (union `EventConnection | RunNotFoundError | PythonError`).

Job names are agreed here and created in the pipeline tasks T9 and T10.
"""
import os
from typing import Any

import requests

GIT_JOB = "git_job"
JIRA_JOB = "jira_job"
CONFLUENCE_JOB = "confluence_job"
JOB_BY_KIND = {"git": GIT_JOB, "jira": JIRA_JOB, "confluence": CONFLUENCE_JOB}
PARTITIONS_DEF = "repos"
PARTITION_TAG = "dagster/partition"

_STATUS = {
    "QUEUED": "queued", "NOT_STARTED": "queued",
    "STARTING": "running", "MANAGED": "running", "STARTED": "running",
    "SUCCESS": "succeeded", "FAILURE": "failed", "CANCELING": "canceled", "CANCELED": "canceled",
}
ACTIVE = ("queued", "running")


def map_status(dagster_state: str) -> str:
    """Dagster run state to `runs.status`. An unknown state counts as `running` (the safe, active value)."""
    return _STATUS.get(str(dagster_state).rsplit(".", 1)[-1].upper(), "running")


class DagsterError(Exception):
    """A Dagster call failed. The message is short and holds no secret."""


class DagsterUnavailable(DagsterError):
    """Dagster cannot be reached."""


_LOGS_QUERY = """
query ControlRunLogs($runId: ID!, $cursor: String, $limit: Int) {
  logsForRun(runId: $runId, afterCursor: $cursor, limit: $limit) {
    __typename
    ... on EventConnection { events { __typename ... on MessageEvent { message timestamp level } } cursor hasMore }
    ... on RunNotFoundError { message }
    ... on PythonError { message }
  }
}
"""
_ADD_PARTITION = """
mutation ControlAddPartition($sel: RepositorySelector!, $def: String!, $key: String!) {
  addDynamicPartition(repositorySelector: $sel, partitionsDefName: $def, partitionKey: $key) {
    __typename
    ... on DuplicateDynamicPartitionError { message }
    ... on UnauthorizedError { message }
    ... on PythonError { message }
  }
}
"""
_DELETE_PARTITION = """
mutation ControlDeletePartition($sel: RepositorySelector!, $def: String!, $keys: [String!]!) {
  deleteDynamicPartitions(repositorySelector: $sel, partitionsDefName: $def, partitionKeys: $keys) {
    __typename
    ... on UnauthorizedError { message }
    ... on UnsupportedOperationError { message }
    ... on PythonError { message }
  }
}
"""


class DagsterClient:
    def __init__(self, graphql: Any, location: str | None = None, repository: str | None = None):
        self._gql = graphql
        self._location = location
        self._repository = repository

    @classmethod
    def from_env(cls) -> "DagsterClient":
        from dagster_graphql import DagsterGraphQLClient

        host = os.environ.get("DAGSTER_GRAPHQL_HOST", "localhost")
        port = int(os.environ.get("DAGSTER_GRAPHQL_PORT", "3000"))
        try:
            graphql = DagsterGraphQLClient(host, port_number=port, timeout=30)
        except Exception as e:
            raise DagsterUnavailable("dagster is unreachable") from e
        return cls(graphql, os.environ.get("DAGSTER_REPOSITORY_LOCATION") or None,
                   os.environ.get("DAGSTER_REPOSITORY_NAME") or None)

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DagsterError:
            raise
        except Exception as e:
            cause = e.__cause__
            if isinstance(e, requests.exceptions.RequestException) or isinstance(cause, requests.exceptions.RequestException):
                raise DagsterUnavailable("dagster is unreachable") from e
            # type name only: the client message embeds the query and variables
            code = e.args[0] if e.args and isinstance(e.args[0], str) and len(e.args[0]) < 60 else type(e).__name__
            raise DagsterError(f"dagster call failed: {code}") from e

    def launch(self, job_name: str, tags: dict[str, str] | None = None, run_config: dict | None = None) -> str:
        return self._call(self._gql.submit_job_execution, job_name, run_config=run_config or {}, tags=tags or {})

    def get_status(self, run_id: str) -> str:
        """Dagster state name, for example `STARTED`. Pass it to `map_status`."""
        return self._call(self._gql.get_run_status, run_id).value

    def terminate(self, run_id: str) -> None:
        self._call(self._gql.terminate_run, run_id)

    def _selector(self) -> dict:
        if not (self._location and self._repository):
            infos = self._call(self._gql._get_repo_locations_and_names_with_pipeline, GIT_JOB)
            if len(infos) != 1:
                raise DagsterError(f"dagster call failed: {GIT_JOB} not found")
            self._location, self._repository = infos[0].repository_location_name, infos[0].repository_name
        return {"repositoryLocationName": self._location, "repositoryName": self._repository}

    def _mutate(self, query: str, variables: dict, field: str, ok: tuple[str, ...]) -> None:
        result = self._call(self._gql._execute, query, variables)[field]
        if result["__typename"] not in ok:
            raise DagsterError(f"dagster call failed: {result['__typename']}")

    def add_partition(self, key: str) -> None:
        """Add a key to the dynamic partitions `repos`. A duplicate is not an error."""
        self._mutate(_ADD_PARTITION, {"sel": self._selector(), "def": PARTITIONS_DEF, "key": key},
                     "addDynamicPartition", ("AddDynamicPartitionSuccess", "DuplicateDynamicPartitionError"))

    def remove_partition(self, key: str) -> None:
        self._mutate(_DELETE_PARTITION, {"sel": self._selector(), "def": PARTITIONS_DEF, "keys": [key]},
                     "deleteDynamicPartitions", ("DeleteDynamicPartitionsSuccess",))

    def get_logs(self, run_id: str, limit: int = 200, page: int = 500, max_pages: int = 20) -> list[str]:
        """The last `limit` log messages, oldest first."""
        lines: list[str] = []
        cursor = None
        for _ in range(max_pages):
            res = self._call(self._gql._execute, _LOGS_QUERY,
                             {"runId": run_id, "cursor": cursor, "limit": page})["logsForRun"]
            if res["__typename"] != "EventConnection":
                raise DagsterError(f"dagster call failed: {res['__typename']}")
            lines += [f"{e['timestamp']} {e['level']} {e['message']}" for e in res["events"] if "message" in e]
            lines = lines[-limit:]
            if not res["hasMore"]:
                break
            cursor = res["cursor"]
        return lines
