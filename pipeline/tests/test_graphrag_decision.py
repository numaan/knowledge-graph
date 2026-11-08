"""The Leiden library decision (docs/streams/graphrag-communities.md) must match the declared extra."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PIN = "graspologic-native==1.3.1"


def _extra() -> list[str]:
    data = tomllib.loads((ROOT / "pipeline" / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["optional-dependencies"]["graphrag"]


def test_graphrag_extra_is_declared_with_one_exact_pin():
    assert _extra() == [PIN]


def test_decision_note_names_the_pin_and_the_licence():
    note = (ROOT / "docs" / "streams" / "graphrag-communities.md").read_text(encoding="utf-8")
    assert PIN in note
    assert "MIT" in note
    assert "GPL" in note  # python-igraph and leidenalg are named as rejected


def test_ci_pytest_job_installs_the_graphrag_extra():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    installs = [line for line in ci.splitlines() if "pip install -e" in line]
    assert any("graphrag" in line for line in installs)
