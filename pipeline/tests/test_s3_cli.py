"""kg-s3 CLI through main(): fake client and fake writer, no boto3, no network, no Neo4j."""
import json
import re
import tomllib
from pathlib import Path

import pytest

from kg_pipeline.sources import s3
from tests.test_s3_sync import BODY, Client, MemWriter, obj


@pytest.fixture
def cli(monkeypatch):
    env = {"w": MemWriter(), "client": Client([obj("docs/a.md"), obj("docs/b.md", etag='"f"')]), "built": []}

    class W:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return env["w"]

        def __exit__(self, *a):
            return False

    def build(config, secret, ui_origin=False):
        env["built"].append((config, secret, ui_origin))
        return env["client"]

    monkeypatch.setattr(s3, "build_client", build)
    monkeypatch.setattr("kg_pipeline.graph.neo4j_writer.Neo4jWriter", W)
    monkeypatch.setattr("kg_pipeline.requirements.read_document", lambda p: BODY.decode())
    monkeypatch.setattr("kg_pipeline.sources.history.link_references", lambda w: 0)
    monkeypatch.setenv("NEO4J_PASSWORD", "x")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    return env


def test_sync_prints_stats_json(cli, capsys):
    assert s3.main(["--bucket", "B", "--prefix", "docs/", "--endpoint", "https://minio.example.com",
                    "--include", "*.md", "--include", "x/*", "--name", "mine"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["new"] == 2 and out["documents"] == 2
    config, secret, ui = cli["built"][0]
    assert config == {"bucket": "B", "prefix": "docs/", "include": ["*.md", "x/*"], "endpoint": "https://minio.example.com"}
    assert secret is None and ui is False
    assert set(cli["w"].docs) == {"s3:mine:docs/a.md", "s3:mine:docs/b.md"}


def test_env_credentials_and_region(cli, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AK")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SK")
    assert s3.main(["--bucket", "b", "--region", "eu-west-1"]) == 0
    config, secret, _ = cli["built"][0]
    assert secret == {"access_key_id": "AK", "secret_access_key": "SK"} and config["region"] == "eu-west-1"


def test_default_name_is_a_valid_slug(cli):
    cli["client"].objects = [obj("Team A/Docs/a.md")]
    s3.main(["--bucket", "My_Bucket.01", "--prefix", "Team A/Docs/"])
    assert list(cli["w"].docs)[0].startswith("s3:my-bucket-01-team-a-docs:")
    from kg_pipeline.control.validate import validate_name
    assert validate_name(s3.default_name("My_Bucket.01", "Team A/Docs/"))
    assert validate_name(s3.default_name("b" * 70, "p" * 70))
    assert validate_name(s3.default_name("___", ""))


def test_dry_run_writes_nothing(cli, capsys):
    cli["w"].docs["s3:n:docs/gone.md"] = {"id": "s3:n:docs/gone.md", "collection": "n", "source": "s3", "version": "v"}
    assert s3.main(["--bucket", "B", "--prefix", "docs/", "--name", "n", "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["new"] == 2 and out["removed"] == 1
    assert cli["client"].downloads == [] and cli["w"].writes == []
    assert list(cli["w"].docs) == ["s3:n:docs/gone.md"]
    assert not any("DELETE" in q or "SET" in q for q, _ in cli["w"].queries)


def test_full_rewrites_unchanged_objects(cli, capsys):
    args = ["--bucket", "B", "--prefix", "docs/", "--name", "n"]
    s3.main(args)
    capsys.readouterr()
    s3.main(args)
    assert json.loads(capsys.readouterr().out)["unchanged"] == 2
    cli["client"].downloads.clear()
    s3.main(args + ["--full"])
    assert len(cli["client"].downloads) == 2


def test_force_passes_the_guard(cli, capsys):
    for i in range(30):
        cli["w"].docs[f"s3:n:docs/old{i}.md"] = {"id": f"s3:n:docs/old{i}.md", "collection": "n", "source": "s3", "version": "v"}
    assert s3.main(["--bucket", "B", "--prefix", "docs/", "--name", "n"]) == 1
    assert "kg-s3:" in capsys.readouterr().err
    assert s3.main(["--bucket", "B", "--prefix", "docs/", "--name", "n", "--force"]) == 0
    assert json.loads(capsys.readouterr().out)["removed"] == 30


def test_failures_exit_1(cli, capsys):
    cli["client"].fail_list = True
    assert s3.main(["--bucket", "B"]) == 1
    assert "boom" in capsys.readouterr().err
    assert s3.main(["--bucket", "B", "--name", "Bad Name"]) == 1
    assert capsys.readouterr().out == ""


def test_missing_boto3_names_the_extra(monkeypatch, capsys):
    monkeypatch.setitem(__import__("sys").modules, "boto3", None)
    assert s3.main(["--bucket", "B"]) == 1
    assert "kg-pipeline[s3]" in capsys.readouterr().err


def test_bucket_is_required(capsys):
    with pytest.raises(SystemExit) as e:
        s3.main([])
    assert e.value.code == 2


def test_help_says_operator_tool(capsys):
    with pytest.raises(SystemExit):
        s3.main(["--help"])
    assert "ui_origin=False" in capsys.readouterr().out


def test_pyproject_and_dockerfile():
    root = Path(__file__).parent.parent
    p = tomllib.loads((root / "pyproject.toml").read_text())
    assert p["project"]["scripts"]["kg-s3"] == "kg_pipeline.sources.s3:main"
    assert re.fullmatch(r"boto3==\d+\.\d+\.\d+", p["project"]["optional-dependencies"]["s3"][0])
    assert "[dagster,control,s3]" in (root / "Dockerfile").read_text()


def test_cli_retries_with_the_region_hint(cli, capsys, monkeypatch):
    seen = []
    real_list = cli["client"].list_objects_v2

    def build(config, secret, ui_origin=False):
        seen.append(config.get("region"))
        return cli["client"]
    monkeypatch.setattr(s3, "build_client", build)
    left = [s3.RedirectRefused("301", region="eu-west-3")]

    def list_v2(**kw):
        if left:
            raise left.pop()
        return real_list(**kw)
    cli["client"].list_objects_v2 = list_v2
    assert s3.main(["--bucket", "B", "--prefix", "docs/", "--name", "n"]) == 0
    assert seen == [None, "eu-west-3"] and json.loads(capsys.readouterr().out)["new"] == 2
