"""Doc check: the GraphRAG names, tools and env vars appear in the docs."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = (ROOT / "docs" / "GUIDE.md").read_text(encoding="utf-8")
RUNBOOK = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
MCP_README = (ROOT / "mcp" / "README.md").read_text(encoding="utf-8")
PIPELINE_README = (ROOT / "pipeline" / "README.md").read_text(encoding="utf-8")

NAMES = ["global_search", "local_search", "kg-communities", "graphrag_job", "graphrag_daily"]
ENV = [
    "KG_COMMUNITY_RESOLUTION", "KG_COMMUNITY_LEVELS", "KG_COMMUNITY_SEED", "KG_COMMUNITY_MAX_NODES",
    "KG_COMMUNITY_MAX_EDGES", "KG_COMMUNITY_BACKEND", "COMMUNITY_LLM", "COMMUNITY_BUDGET",
]


def test_guide_and_runbook_name_everything():
    for name, text in (("GUIDE", GUIDE), ("RUNBOOK", RUNBOOK)):
        for word in NAMES + ENV:
            assert word in text, f"{word} missing in {name}"


def test_mcp_readme_lists_both_tools():
    for tool in ("global_search", "local_search"):
        assert f"`{tool}`" in MCP_README


def test_pipeline_readme_lists_script():
    assert "kg-communities" in PIPELINE_README


def test_admin_trigger_is_a_follow_up():
    for text in (GUIDE, RUNBOOK):
        assert re.search(r"admin[^\n]{0,200}follow-up|follow-up[^\n]{0,200}admin", text, re.I)


def test_runbook_covers_decisions_and_unverified_list():
    for word in ("graspologic-native==1.3.1", "not yet verified", "repo:<name>"):
        assert word.lower() in (RUNBOOK + GUIDE).lower(), word


def test_runbook_states_acl_window_and_rule():
    text = RUNBOOK.lower()
    assert "acl window" in text
    assert "narrow an acl, then run `graphrag_job`" in text
