"""Agent benchmark: run a headless coding agent on precept tasks with and without the kg MCP server.

    python bench/run.py --dry-run                                   # prepare copies, check that every task fails
    python bench/run.py --task revoke-reason --variant baseline     # one run, no MCP server
    python bench/run.py --repeat 3 --mcp-config bench/mcp-kg.json   # every task, both variants, 3 times

Each run copies precept (read only) to a new temporary folder, runs `claude -p` there with the task
prompt, then copies the hidden check (checks/<file> -> backend/tests/test_bench_check.py) and runs it.
Recorded per run (results.jsonl): success, tool calls (all and kg), turns, tokens, cost and time.
The `kg` variant needs a kg MCP server with precept ingested; the baseline gets no MCP server at all.
Needs: the `claude` CLI, PyYAML, and a Python with the precept backend requirements (--python).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
IGNORE = shutil.ignore_patterns("node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", "*.pptx", "*.zip")
CHECK_FILE = "backend/tests/test_bench_check.py"
# Default tools: read and edit files, and run the backend tests. No network, no package installs.
ALLOWED = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(python -m pytest:*)", "Bash(pytest:*)", "Bash(git diff:*)",
           "Bash(git status:*)", "mcp__kg"]
KG_HINT = ("\n\nA knowledge graph of this repository is available through the `kg` MCP tools (search_graph, "
           "find_symbol, impact_analysis, find_related_tests, ...). Use them to find code before you read files.")


def load_tasks(names: list[str] | None) -> list[dict]:
    tasks = [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted((HERE / "tasks").glob("*.yaml"))]
    if names:
        tasks = [t for t in tasks if t["id"] in names]
        missing = set(names) - {t["id"] for t in tasks}
        if missing:
            raise SystemExit(f"unknown tasks: {sorted(missing)}")
    return tasks


def prepare(src: Path, task: dict, variant: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix=f"kgbench-{task['id']}-{variant}-"))
    shutil.copytree(src, root / "precept", ignore=IGNORE)
    return root / "precept"


def remove(folder: Path) -> None:
    """Delete a temporary copy; git marks its object files read-only on Windows."""
    def writable(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        func(path)
    try:
        shutil.rmtree(folder, onexc=writable)
    except OSError as e:  # a process the agent started may still hold the folder
        print(f"warning: could not remove {folder}: {e}", file=sys.stderr)


def run_check(work: Path, task: dict, python: str, timeout: int = 600) -> dict:
    tests = work / "backend" / "tests"
    shutil.copy(HERE / "checks" / "common.py", tests / "bench_common.py")
    shutil.copy(HERE / "checks" / task["check"], work / CHECK_FILE)
    t0 = time.time()
    try:
        p = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_bench_check.py"],
                           cwd=work / "backend", capture_output=True, text=True, timeout=timeout)
        ok, tail = p.returncode == 0, (p.stdout + p.stderr)[-1500:]
    except subprocess.TimeoutExpired:
        ok, tail = False, "check timed out"
    return {"success": ok, "check_seconds": round(time.time() - t0, 1), "check_output": tail}


def parse_stream(lines: list[str]) -> dict:
    """Tool calls, turns, tokens and cost from `claude -p --output-format stream-json --verbose`."""
    tools: dict[str, int] = {}
    result: dict = {}
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    tools[block["name"]] = tools.get(block["name"], 0) + 1
        elif ev.get("type") == "result":
            result = ev
    usage = result.get("usage") or {}
    return {"tool_calls": sum(tools.values()), "kg_tool_calls": sum(n for k, n in tools.items() if k.startswith("mcp__kg__")),
            "tools": tools, "turns": result.get("num_turns"), "agent_error": result.get("is_error"),
            "input_tokens": usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0), "cost_usd": result.get("total_cost_usd"),
            "agent_seconds": round((result.get("duration_ms") or 0) / 1000, 1)}


def run_agent(work: Path, task: dict, variant: str, args) -> dict:
    cfg = work.parent / "mcp.json"
    if variant == "kg":
        cfg.write_text(Path(args.mcp_config).read_text(encoding="utf-8"), encoding="utf-8")
    else:
        cfg.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    prompt = task["prompt"] + (KG_HINT if variant == "kg" else "")
    cmd = [args.claude, "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", args.model,
           "--max-turns", str(args.max_turns), "--strict-mcp-config", "--mcp-config", str(cfg),
           "--permission-mode", "acceptEdits", "--allowedTools", ",".join(ALLOWED)]
    if args.max_budget_usd:
        cmd += ["--max-budget-usd", str(args.max_budget_usd)]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=args.timeout, encoding="utf-8")
        out = p.stdout.splitlines()
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace").splitlines() if isinstance(e.stdout, bytes) else (e.stdout or "").splitlines()
    (work.parent / "transcript.jsonl").write_text("\n".join(out), encoding="utf-8")
    return {**parse_stream(out), "wall_seconds": round(time.time() - t0, 1)}


def summary(rows: list[dict]) -> str:
    out = ["| Variant | Runs | Success | Tool calls | kg calls | Input tokens | Output tokens | Cost USD | Seconds |",
           "|---|---|---|---|---|---|---|---|---|"]
    for variant in sorted({r["variant"] for r in rows}):
        rs = [r for r in rows if r["variant"] == variant]
        mean = lambda k: sum((r.get(k) or 0) for r in rs) / len(rs)
        out.append(f"| {variant} | {len(rs)} | {sum(r['success'] for r in rs) / len(rs):.0%} | {mean('tool_calls'):.1f} | "
                   f"{mean('kg_tool_calls'):.1f} | {mean('input_tokens'):,.0f} | {mean('output_tokens'):,.0f} | "
                   f"{mean('cost_usd'):.3f} | {mean('wall_seconds'):.0f} |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--precept", default=os.environ.get("PRECEPT_PATH", "C:/workspace/precept"), help="source checkout (read only)")
    ap.add_argument("--task", action="append", help="task id (default: all)")
    ap.add_argument("--variant", action="append", choices=["baseline", "kg"], help="default: both")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--max-budget-usd", type=float, default=2.0, help="per run")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per agent run")
    ap.add_argument("--mcp-config", default=str(HERE / "mcp-kg.json"), help="MCP config with the kg server (kg variant)")
    ap.add_argument("--claude", default="claude", help="path of the claude CLI")
    ap.add_argument("--python", default=sys.executable, help="Python with the precept backend requirements, for checks")
    ap.add_argument("--out", default="bench-results.jsonl")
    ap.add_argument("--keep", action="store_true", help="keep the temporary copies")
    ap.add_argument("--dry-run", action="store_true", help="no agent: check that each task fails on the unchanged code")
    args = ap.parse_args(argv)
    src = Path(args.precept)
    tasks = load_tasks(args.task)
    variants = args.variant or ["baseline", "kg"]
    if "kg" in variants and not args.dry_run and not Path(args.mcp_config).exists():
        raise SystemExit(f"{args.mcp_config} not found: copy bench/mcp-kg.json.example and set the kg server URL")

    rows = []
    for task in tasks:
        for variant in (["none"] if args.dry_run else variants):
            for i in range(args.repeat):
                work = prepare(src, task, variant)
                row = {"task": task["id"], "variant": variant, "repeat": i, "model": args.model}
                if not args.dry_run:
                    row |= run_agent(work, task, variant, args)
                row |= run_check(work, task, args.python)
                if args.dry_run:
                    row["valid_task"] = not row["success"]  # an unsolved task must fail its check
                print(json.dumps({k: v for k, v in row.items() if k != "check_output"}), flush=True)
                rows.append(row)
                with open(args.out, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
                if not args.keep:
                    remove(work.parent)
    if args.dry_run:
        bad = [r["task"] for r in rows if not r["valid_task"]]
        print("all tasks fail on the unchanged code" if not bad else f"checks pass without a change: {bad}")
        sys.exit(1 if bad else 0)
    print(summary(rows))


if __name__ == "__main__":
    main()
