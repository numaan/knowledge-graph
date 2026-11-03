"""Online logical backup and restore of the Neo4j graph (Community has no online neo4j-admin backup).

    kg-backup dump --out /archive/backups/neo4j --keep 7     # writes neo4j-<UTC time>.jsonl.gz
    kg-backup restore neo4j-20261104T023000Z.jsonl.gz        # into an empty database
    kg-backup check neo4j-20261104T023000Z.jsonl.gz          # read the file, print the counts

File format (gzip JSON lines): a header with the schema statements, one line per node
{"n": element id, "l": labels, "p": properties}, one line per relationship {"r": type, "s": start,
"e": end, "p": properties}, and an end line with the counts (a file without it is incomplete).
Nodes and relationships are read in one read transaction while the pipeline may still write; run the
backup when the schedules are quiet (the Helm CronJob default is 02:30).
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from neo4j import time as neo4j_time

from .graph.neo4j_writer import Neo4jWriter, _safe

log = logging.getLogger(__name__)
FORMAT, VERSION = "kg-backup", 1
BATCH = 5000
TEMP_LABEL = "KgRestoreTmp"  # marks restored nodes until their relationships exist
_TEMPORAL = {"DateTime": neo4j_time.DateTime, "Date": neo4j_time.Date, "Time": neo4j_time.Time}


def encode(value):
    """JSON-safe form of a property value; temporal values keep their type."""
    for name, cls in _TEMPORAL.items():
        if isinstance(value, cls):
            return {"$t": name, "v": value.iso_format()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)  # durations, points: kept as text


def decode(value):
    if isinstance(value, dict) and "$t" in value:
        return _TEMPORAL[value["$t"]].from_iso_format(value["v"])
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def idempotent(statement: str) -> str:
    """CREATE ... INDEX `x` FOR ... -> CREATE ... INDEX `x` IF NOT EXISTS FOR ..."""
    if "IF NOT EXISTS" in statement:
        return statement
    return re.sub(r"^(CREATE (?:\w+ )*?(?:CONSTRAINT|INDEX) `[^`]+`)", r"\1 IF NOT EXISTS", statement, count=1)


def schema_statements(w: Neo4jWriter) -> list[str]:
    constraints = [r["s"] for r in w._run("SHOW CONSTRAINTS YIELD createStatement AS s").records]
    indexes = [r["s"] for r in w._run("SHOW INDEXES YIELD type, owningConstraint, createStatement AS s "
                                      "WHERE type <> 'LOOKUP' AND owningConstraint IS NULL RETURN s").records]
    return constraints + indexes


def dump(w: Neo4jWriter, path: Path) -> dict:
    counts = {"nodes": 0, "relationships": 0}
    tmp = path.with_suffix(path.suffix + ".part")
    with gzip.open(tmp, "wt", encoding="utf-8") as out, w.driver.session(database=w.database) as session:
        out.write(json.dumps({"format": FORMAT, "version": VERSION, "created": datetime.now(timezone.utc).isoformat(),
                              "schema": schema_statements(w)}) + "\n")
        with session.begin_transaction() as tx:  # one transaction: relationships match the nodes read
            for r in tx.run("MATCH (n) RETURN elementId(n) AS id, labels(n) AS l, properties(n) AS p"):
                out.write(json.dumps({"n": r["id"], "l": r["l"], "p": {k: encode(v) for k, v in r["p"].items()}}) + "\n")
                counts["nodes"] += 1
            for r in tx.run("MATCH (a)-[x]->(b) RETURN type(x) AS t, elementId(a) AS s, elementId(b) AS e, properties(x) AS p"):
                out.write(json.dumps({"r": r["t"], "s": r["s"], "e": r["e"],
                                      "p": {k: encode(v) for k, v in r["p"].items()}}) + "\n")
                counts["relationships"] += 1
        out.write(json.dumps({"end": True, **counts}) + "\n")
    tmp.replace(path)  # a file with the final name is always complete
    return {"file": str(path), "bytes": path.stat().st_size, **counts}


def read(path: Path):
    """(header, iterator of lines); raises ValueError on a file that is not a kg-backup."""
    fh = gzip.open(path, "rt", encoding="utf-8")
    header = json.loads(fh.readline())
    if header.get("format") != FORMAT or header.get("version") != VERSION:
        fh.close()
        raise ValueError(f"{path} is not a {FORMAT} v{VERSION} file")

    def lines():
        with fh:
            for line in fh:
                yield json.loads(line)
    return header, lines()


def check(path: Path) -> dict:
    header, lines = read(path)
    counts, end = {"nodes": 0, "relationships": 0}, None
    for row in lines:
        if "n" in row:
            counts["nodes"] += 1
        elif "r" in row:
            counts["relationships"] += 1
        elif row.get("end"):
            end = row
    if end is None or end["nodes"] != counts["nodes"] or end["relationships"] != counts["relationships"]:
        raise ValueError(f"{path} is incomplete: read {counts}, end line {end}")
    return {"file": str(path), "created": header["created"], "schema": len(header["schema"]), **counts}


def _flush_nodes(w: Neo4jWriter, groups: dict) -> None:
    for labels, rows in groups.items():
        extra = "".join(f":{_safe(l)}" for l in labels)
        w._run(f"UNWIND $rows AS row CREATE (n:{TEMP_LABEL}{extra}) SET n = row.p, n._bid = row.b", rows=rows)
    groups.clear()


def _flush_rels(w: Neo4jWriter, groups: dict) -> None:
    for rtype, rows in groups.items():
        w._run(f"UNWIND $rows AS row MATCH (a:{TEMP_LABEL} {{_bid: row.s}}) MATCH (b:{TEMP_LABEL} {{_bid: row.e}}) "
               f"CREATE (a)-[x:{_safe(rtype)}]->(b) SET x = row.p", rows=rows)
    groups.clear()


def restore(w: Neo4jWriter, path: Path, force: bool = False) -> dict:
    check(path)  # never load half a file
    existing = w._run("MATCH (n) RETURN count(n) AS c").records[0]["c"]
    if existing and not force:
        raise SystemExit(f"the database has {existing} nodes; restore needs an empty database (or --force to add)")
    header, lines = read(path)
    w._run(f"CREATE INDEX kg_restore_bid IF NOT EXISTS FOR (n:{TEMP_LABEL}) ON (n._bid)")
    w._run("CALL db.awaitIndexes(300)")
    nodes, rels, counts, pending = defaultdict(list), defaultdict(list), {"nodes": 0, "relationships": 0}, 0
    for row in lines:
        if "n" in row:
            nodes[tuple(row["l"])].append({"b": row["n"], "p": {k: decode(v) for k, v in row["p"].items()}})
            counts["nodes"] += 1
        elif "r" in row:
            if nodes:
                _flush_nodes(w, nodes)  # the file lists all nodes first
            rels[row["r"]].append({"s": row["s"], "e": row["e"], "p": {k: decode(v) for k, v in row["p"].items()}})
            counts["relationships"] += 1
        else:
            continue
        pending += 1
        if pending >= BATCH:
            _flush_nodes(w, nodes)
            _flush_rels(w, rels)
            pending = 0
    _flush_nodes(w, nodes)
    _flush_rels(w, rels)
    with w.driver.session(database=w.database) as session:  # CALL IN TRANSACTIONS needs an implicit transaction
        session.run(f"MATCH (n:{TEMP_LABEL}) CALL (n) {{ REMOVE n:{TEMP_LABEL}, n._bid }} "
                    "IN TRANSACTIONS OF 10000 ROWS").consume()
    w._run("DROP INDEX kg_restore_bid IF EXISTS")
    for statement in header["schema"]:  # after the data: bulk inserts are faster without indexes
        w._run(idempotent(statement))
    w._run("CALL db.awaitIndexes(600)")
    return {"file": str(path), "schema": len(header["schema"]), **counts}


def prune(folder: Path, keep: int) -> list[str]:
    """Delete all but the newest `keep` backups in folder; returns the deleted names."""
    files = sorted(folder.glob("neo4j-*.jsonl.gz"))
    gone = files[:-keep] if keep > 0 else []
    for f in gone:
        f.unlink()
    return [f.name for f in gone]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump", help="write a backup file")
    d.add_argument("--out", type=Path, required=True, help="folder for the backup files")
    d.add_argument("--keep", type=int, default=0, help="keep only the newest N files in the folder (0 = all)")
    r = sub.add_parser("restore", help="load a backup file into an empty database")
    r.add_argument("file", type=Path)
    r.add_argument("--force", action="store_true", help="restore into a database that already has nodes")
    c = sub.add_parser("check", help="verify that a backup file is complete")
    c.add_argument("file", type=Path)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.cmd == "check":
        print(json.dumps(check(args.file), indent=2))
        return
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        if args.cmd == "dump":
            args.out.mkdir(parents=True, exist_ok=True)
            out = dump(w, args.out / f"neo4j-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl.gz")
            out["pruned"] = prune(args.out, args.keep)
        else:
            out = restore(w, args.file, args.force)
    json.dump(out, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
