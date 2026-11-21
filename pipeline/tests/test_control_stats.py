"""DagsterClient.get_stats: asset materialization metadata of a run, as a JSON-safe dict."""
import json

from dagster_graphql.schema import create_schema
from graphql import parse, validate

from kg_pipeline.control import dagster_client as dc


class Gql:
    def __init__(self, pages=None, error=None):
        self.pages = list(pages or [])
        self.error = error
        self.calls = []

    def _execute(self, query, variables=None):
        self.calls.append((query, variables))
        if self.error:
            raise self.error
        return self.pages.pop(0)


def entry(path, *entries, partition=None):
    return {"__typename": "MaterializationEvent", "assetKey": {"path": path}, "partition": partition,
            "metadataEntries": list(entries)}


def page(events, more=False, cursor="c"):
    return {"logsForRun": {"__typename": "EventConnection", "events": events, "cursor": cursor, "hasMore": more}}


def test_query_is_valid_against_installed_schema():
    errors = validate(create_schema().graphql_schema, parse(dc._STATS_QUERY))
    assert errors == []


def test_get_stats_maps_entries_by_asset_key():
    ev = [
        {"__typename": "ExecutionStepStartEvent"},
        entry(["code_snapshot"], {"__typename": "IntMetadataEntry", "label": "nodes", "intValue": 5, "intRepr": "5"},
              {"__typename": "TextMetadataEntry", "label": "commit", "text": "abc"},
              {"__typename": "FloatMetadataEntry", "label": "f", "floatValue": 1.5, "floatRepr": "1.5"},
              {"__typename": "BoolMetadataEntry", "label": "ok", "boolValue": True},
              {"__typename": "JsonMetadataEntry", "label": "by", "jsonString": '{"CALLS": 3}'}),
        entry(["a", "b"], {"__typename": "IntMetadataEntry", "label": "big", "intValue": None, "intRepr": "99999999999"}),
    ]
    c = dc.DagsterClient(Gql([page(ev)]))
    stats = c.get_stats("run-1")
    assert stats == {"code_snapshot": {"nodes": 5, "commit": "abc", "f": 1.5, "ok": True, "by": {"CALLS": 3}},
                     "a/b": {"big": 99999999999}}
    json.dumps(stats)


def test_get_stats_follows_pages_and_keeps_latest_per_key():
    pages = [page([entry(["x"], {"__typename": "IntMetadataEntry", "label": "n", "intValue": 1, "intRepr": "1"})],
                  more=True),
             page([entry(["x"], {"__typename": "IntMetadataEntry", "label": "n", "intValue": 2, "intRepr": "2"})])]
    assert dc.DagsterClient(Gql(pages)).get_stats("r") == {"x": {"n": 2}}


def test_get_stats_includes_partition_key_when_present():
    ev = [entry(["code_snapshot"], {"__typename": "IntMetadataEntry", "label": "n", "intValue": 1, "intRepr": "1"},
                partition="repo-a")]
    assert dc.DagsterClient(Gql([page(ev)])).get_stats("r") == {"code_snapshot": {"n": 1}}


def test_get_stats_returns_none_on_failure_or_no_data():
    assert dc.DagsterClient(Gql(error=RuntimeError("boom"))).get_stats("r") is None
    assert dc.DagsterClient(Gql([{"logsForRun": {"__typename": "RunNotFoundError", "message": "m"}}])).get_stats("r") is None
    assert dc.DagsterClient(Gql([page([{"__typename": "ExecutionStepStartEvent"}])])).get_stats("r") is None


def test_unknown_entry_types_are_skipped():
    ev = [entry(["x"], {"__typename": "TableMetadataEntry", "label": "t"},
                {"__typename": "TextMetadataEntry", "label": "k", "text": "v"})]
    assert dc.DagsterClient(Gql([page(ev)])).get_stats("r") == {"x": {"k": "v"}}
