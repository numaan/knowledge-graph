"""Database schemas -> :Table and :Column nodes, and code -> table edges.

Schema sources (in this order; later ones add columns to the same table):
    SQL migrations      CREATE TABLE, ALTER TABLE ADD/DROP COLUMN, DROP TABLE, REFERENCES (.sql files, sorted by path)
    Alembic             op.create_table / op.add_column / op.drop_column in Python migrations
    Prisma              model blocks in .prisma files (@@map, @map)
    ORM models          SQLAlchemy (__tablename__, Column / mapped_column), Django (models.Model, Meta.db_table),
                        JPA / Hibernate (@Entity, @Table(name)), TypeORM (@Entity("name")), EF ([Table("name")])

Edges:
    Table -HAS_COLUMN-> Column, Table -FOREIGN_KEY-> Table, File -DEFINES / MIGRATES-> Table
    ORM class -MAPS_TO-> Table (also BomClass -MAPS_TO-> Table through the ODM BOM -> XOM mapping)
    Symbol -READS / WRITES-> Table from literal SQL in strings and from cheap ORM calls
    (Model.objects.filter, session.query(Model), prisma.model.findMany, Spring Data repositories)

Edges from names in code use method 'heuristic' with a confidence; schema edges use 'parser'.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from .files import SourceFile
from .model import CodeGraph
from .treesitter import FileSyntax

_ID = r"[`\"\[]?([\w$]+)[`\"\]]?"
_QNAME = rf"(?:{_ID}\.)?{_ID}"     # optional schema
CREATE = re.compile(rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:GLOBAL\s+|LOCAL\s+)?(?:TEMP|TEMPORARY|UNLOGGED)\s+)?TABLE\s+"
                    rf"(?:IF\s+NOT\s+EXISTS\s+)?{_QNAME}\s*\(", re.I)
ALTER = re.compile(rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_QNAME}\s+(.*?);", re.I | re.S)
DROP = re.compile(rf"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_QNAME}", re.I)
REFS = re.compile(rf"\bREFERENCES\s+{_QNAME}", re.I)
CONSTRAINT = re.compile(r"^(CONSTRAINT|PRIMARY|FOREIGN|UNIQUE|CHECK|INDEX|KEY|EXCLUDE|FULLTEXT|SPATIAL)\b", re.I)

ALEMBIC_CREATE = re.compile(r"""op\.create_table\(\s*["'](\w+)["']""")
ALEMBIC_ADD = re.compile(r"""op\.add_column\(\s*["'](\w+)["']\s*,\s*(?:sa\.)?Column\(\s*["'](\w+)["']\s*,\s*(?:sa\.)?(\w+)""")
ALEMBIC_DROP = re.compile(r"""op\.drop_column\(\s*["'](\w+)["']\s*,\s*["'](\w+)["']""")
ALEMBIC_COL = re.compile(r"""(?:sa\.)?Column\(\s*["'](\w+)["']\s*,\s*(?:sa\.)?(\w+)""")

PY_TABLENAME = re.compile(r"""^\s*__tablename__\s*=\s*["'](\w+)["']""", re.M)
PY_COLUMN = re.compile(r"""^\s*(\w+)\s*(?::\s*[^=\n]+)?=\s*(?:sa\.|db\.)?(?:Column|mapped_column)\(\s*(?:["'](\w+)["']\s*,\s*)?(?:sa\.|db\.)?(\w+)?""", re.M)
DJANGO_FIELD = re.compile(r"""^\s*(\w+)\s*=\s*models\.(\w+Field|ForeignKey|OneToOneField)\(""", re.M)
DJANGO_TABLE = re.compile(r"""^\s*db_table\s*=\s*["'](\w+)["']""", re.M)
PRISMA_MODEL = re.compile(r"^\s*model\s+(\w+)\s*\{(.*?)^\s*\}", re.M | re.S)
PRISMA_FIELD = re.compile(r"^\s*(\w+)\s+(\w+)(\[\]|\?)?([^\n]*)", re.M)

SQL_STRING = re.compile(r"""(?P<q>\"\"\"|'''|["'`])(?P<sql>\s*(?:SELECT|INSERT|UPDATE|DELETE|WITH|MERGE)\b.*?)(?P=q)""", re.I | re.S)
SQL_READ = re.compile(rf"(?<!DELETE\s)\b(?:FROM|JOIN)\s+{_QNAME}", re.I)
SQL_WRITE = re.compile(rf"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO)\s+{_QNAME}", re.I)
SQL_WORDS = {"select", "where", "set", "values", "on", "as", "lateral", "unnest", "only", "the", "a"}

READ_OPS = r"(filter|get|all|exclude|first|last|count|exists|values|values_list|aggregate|annotate|order_by|find\w*|aggregate|groupBy|count)"
WRITE_OPS = r"(create|bulk_create|update|update_or_create|get_or_create|delete|bulk_update|createMany|updateMany|deleteMany|upsert)"
DJANGO_USE = re.compile(rf"\b([A-Z]\w*)\.objects\.(?:{READ_OPS}|{WRITE_OPS})\b")
SA_READ = re.compile(r"\b(?:query|select|get|scalars?)\(\s*([A-Z]\w*)\b")
SA_WRITE = re.compile(r"\b(?:add\(\s*|insert\(\s*|update\(\s*|delete\(\s*)([A-Z]\w*)\b")
PRISMA_USE = re.compile(rf"\bprisma\.(\w+)\.(?:{READ_OPS}|{WRITE_OPS})\b")
REPO_SUPER = {"JpaRepository", "CrudRepository", "PagingAndSortingRepository", "ReactiveCrudRepository",
              "ListCrudRepository", "Repository"}


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def _split_top(body: str) -> list[str]:
    """Split a CREATE TABLE body on commas that are not inside parentheses."""
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


class Schema:
    def __init__(self, g: CodeGraph):
        self.g = g
        self.tables: dict[str, str] = {}          # lower name -> table id
        self.models: dict[str, str] = {}          # ORM class short name -> table id

    def table(self, name: str, source: str, file_id: str | None = None, edge: str = "DEFINES") -> str:
        key = name.lower()
        tid = self.tables.get(key) or f"{self.g.repo}:db:{key}"
        if key not in self.tables:
            self.tables[key] = tid
            self.g.add_node(tid, ("Code", "Table"), name=key, sources=[source])
            self.g.counters["db_tables"] += 1
        node = self.g.nodes[tid]
        if source not in node.props["sources"]:
            node.props["sources"] = sorted({*node.props["sources"], source})
        if file_id:
            self.g.add_edge(edge, file_id, tid, "parser")
        return tid

    def column(self, table_id: str, name: str, type_: str | None, **props) -> None:
        cid = f"{table_id}.{name.lower()}"
        self.g.add_node(cid, ("Code", "Column"), name=name.lower(), table=self.g.nodes[table_id].props["name"],
                        data_type=type_.lower() if type_ else None, **props)
        self.g.add_edge("HAS_COLUMN", table_id, cid, "parser")

    def drop_column(self, table_id: str, name: str) -> None:
        cid = f"{table_id}.{name.lower()}"
        self.g.nodes.pop(cid, None)
        self.g.edges.pop(("HAS_COLUMN", table_id, cid), None)

    def lookup(self, name: str) -> str | None:
        """Table id for a table or ORM class name, also trying the plural / singular form."""
        key = name.lower()
        for k in (key, key + "s", key.removesuffix("s"), _snake(name), _snake(name) + "s"):
            if k in self.tables:
                return self.tables[k]
        return self.models.get(name)

    # ---- schema sources ------------------------------------------------------------------------
    def sql(self, f: SourceFile) -> None:
        file_id = f"{self.g.repo}:{f.path}"
        text = re.sub(r"--[^\n]*|/\*.*?\*/", "", f.text, flags=re.S)
        for m in CREATE.finditer(text):
            tid = self.table(m.group(2), "sql", file_id)
            for part in _split_top(text[m.end():]):
                if CONSTRAINT.match(part):
                    for ref in REFS.finditer(part):
                        self.g.add_edge("FOREIGN_KEY", tid, self.table(ref.group(2), "sql"), "parser")
                    continue
                tokens = part.split()
                col = tokens[0].strip('`"[]')
                self.column(tid, col, tokens[1] if len(tokens) > 1 else None,
                            nullable=False if re.search(r"\bNOT\s+NULL\b|\bPRIMARY\s+KEY\b", part, re.I) else None,
                            primary_key=True if re.search(r"\bPRIMARY\s+KEY\b", part, re.I) else None)
                for ref in REFS.finditer(part):
                    self.g.add_edge("FOREIGN_KEY", tid, self.table(ref.group(2), "sql"), "parser", column=col.lower())
        for m in ALTER.finditer(text):
            tid = self.table(m.group(2), "sql", file_id, "MIGRATES")
            for action in _split_top(m.group(3)):
                add = re.match(rf"ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?{_ID}\s+(\w+)", action, re.I)
                drop = re.match(rf"DROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?{_ID}", action, re.I)
                if add and add.group(1).upper() not in {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "INDEX"}:
                    self.column(tid, add.group(1), add.group(2))
                elif drop and drop.group(1).upper() not in {"CONSTRAINT", "INDEX"}:
                    self.drop_column(tid, drop.group(1))
                for ref in REFS.finditer(action):
                    self.g.add_edge("FOREIGN_KEY", tid, self.table(ref.group(2), "sql"), "parser")
        for m in DROP.finditer(text):
            key = m.group(2).lower()
            if key in self.tables:
                self.g.nodes[self.tables[key]].props["dropped"] = True

    def alembic(self, f: SourceFile) -> None:
        file_id = f"{self.g.repo}:{f.path}"
        for m in ALEMBIC_CREATE.finditer(f.text):
            tid = self.table(m.group(1), "alembic", file_id)
            body = f.text[m.end():]
            depth, end = 1, 0
            for end, ch in enumerate(body):
                depth += {"(": 1, ")": -1}.get(ch, 0)
                if depth == 0:
                    break
            for col, type_ in ALEMBIC_COL.findall(body[:end]):
                self.column(tid, col, type_)
        for table, col, type_ in ALEMBIC_ADD.findall(f.text):
            self.column(self.table(table, "alembic", file_id, "MIGRATES"), col, type_)
        for table, col in ALEMBIC_DROP.findall(f.text):
            self.drop_column(self.table(table, "alembic", file_id, "MIGRATES"), col)

    def prisma(self, f: SourceFile) -> None:
        file_id = f"{self.g.repo}:{f.path}"
        for model, body in PRISMA_MODEL.findall(f.text):
            mapped = re.search(r"""@@map\(\s*["'](\w+)["']""", body)
            tid = self.table(mapped.group(1) if mapped else model, "prisma", file_id)
            self.models[model] = tid
            self.models[model[0].lower() + model[1:]] = tid      # prisma client accessor: prisma.invoice
            for name, type_, mod, rest in PRISMA_FIELD.findall(body):
                if name.startswith("@") or mod == "[]" or "@relation" in rest and type_[0].isupper():
                    continue
                col = re.search(r"""@map\(\s*["'](\w+)["']""", rest)
                self.column(tid, col.group(1) if col else name, type_, nullable=True if mod == "?" else None,
                            primary_key=True if "@id" in rest else None)

    def orm(self, f: SourceFile, s: FileSyntax) -> None:
        lines = f.text.splitlines()
        for sym in s.symbols:
            if sym.kind != "Class":
                continue
            body = "\n".join(lines[sym.start_line - 1:sym.end_line])
            anns = {re.split(r"[(\s]", a, maxsplit=1)[0].split(".")[-1]: a for a in sym.annotations}
            supers = set(sym.supertypes)
            name, cols, conf, source = None, [], 0.9, None
            if f.language == "python" and PY_TABLENAME.search(body):
                name, source = PY_TABLENAME.search(body).group(1), "sqlalchemy"
                cols = [(c or attr, t) for attr, c, t in PY_COLUMN.findall(body)]
            elif f.language == "python" and "Model" in supers and DJANGO_FIELD.search(body):
                table = DJANGO_TABLE.search(body)
                app = PurePosixPath(f.path).parent.name if PurePosixPath(f.path).stem == "models" \
                    else PurePosixPath(f.path).parent.parent.name
                name, source = (table.group(1), "django") if table else (f"{app}_{sym.name.lower()}", "django")
                conf = 0.9 if table else 0.7
                cols = [(attr + ("_id" if t in {"ForeignKey", "OneToOneField"} else ""), t)
                        for attr, t in DJANGO_FIELD.findall(body)]
            elif "Entity" in anns or "Table" in anns:
                explicit = re.search(r"""(?:name\s*[=:]\s*)?["'](\w+)["']""", anns.get("Table") or anns.get("Entity") or "")
                name = explicit.group(1) if explicit else _snake(sym.name)
                conf, source = (0.9 if explicit else 0.7), "orm"
                cols = [(c, None) for c in re.findall(r"""@Column\(\s*(?:name\s*=\s*)?["'](\w+)["']""", body)]
            if not name:
                continue
            tid = (self.lookup(name) if conf < 0.9 else None) or self.table(name, source)
            for col, type_ in cols:
                self.column(tid, col, type_)
            self.models[sym.name] = tid
            self.g.add_edge("MAPS_TO", f"{self.g.repo}:{f.path}#{sym.qualname}", tid, "parser", conf)

    # ---- usage -----------------------------------------------------------------------------------
    def usage(self, f: SourceFile, s: FileSyntax) -> None:
        def owner(offset: int) -> str:
            line = f.text.count("\n", 0, offset) + 1
            best = None
            for sym in s.symbols:
                if sym.start_line <= line <= sym.end_line and (best is None or sym.end_line - sym.start_line < best.end_line - best.start_line):
                    best = sym
            return f"{self.g.repo}:{f.path}#{best.qualname}" if best else f"{self.g.repo}:{f.path}"

        def edge(etype: str, offset: int, tid: str | None, conf: float, via: str) -> None:
            if tid:
                self.g.add_edge(etype, owner(offset), tid, "heuristic", conf, via=via)
                self.g.counters[f"db_{etype.lower()}"] += 1

        for m in SQL_STRING.finditer(f.text):
            sql, upper = m.group("sql"), m.group("sql").lstrip()[:6].isupper()
            for pattern, etype in ((SQL_READ, "READS"), (SQL_WRITE, "WRITES")):
                for t in pattern.finditer(sql):
                    name = t.group(2)
                    if name.lower() in SQL_WORDS:
                        continue
                    tid = self.tables.get(name.lower())
                    if tid is None and upper:   # unknown table: only in upper-case SQL
                        tid = self.table(name, "code")
                    edge(etype, m.start(), tid, 0.8 if name.lower() in self.tables else 0.5, "sql")
        for m in DJANGO_USE.finditer(f.text):
            edge("READS" if m.group(2) else "WRITES", m.start(), self.models.get(m.group(1)), 0.7, "orm")
        if f.language == "python":
            for pattern, etype in ((SA_READ, "READS"), (SA_WRITE, "WRITES")):
                for m in pattern.finditer(f.text):
                    edge(etype, m.start(), self.models.get(m.group(1)), 0.6, "orm")
        for m in PRISMA_USE.finditer(f.text):
            edge("READS" if m.group(2) else "WRITES", m.start(), self.models.get(m.group(1)), 0.8, "orm")
        for sym in s.symbols:
            if sym.kind in {"Interface", "Class"} and REPO_SUPER & set(sym.supertypes):
                # JpaRepository<Order, Long>: the entity is the first type argument
                for t in {*sym.supertypes, *re.findall(r"Repository\s*<\s*(\w+)", sym.signature)}:
                    if t in self.models:
                        sid = f"{self.g.repo}:{f.path}#{sym.qualname}"
                        self.g.add_edge("READS", sid, self.models[t], "heuristic", 0.7, via="repository")
                        self.g.add_edge("WRITES", sid, self.models[t], "heuristic", 0.7, via="repository")


def build(g: CodeGraph, files: list[SourceFile], syntax: dict[str, FileSyntax]) -> None:
    db = Schema(g)
    for f in files:                       # sorted by path, so numbered migrations apply in order
        if f.language == "sql":
            db.sql(f)
        elif f.language == "prisma":
            db.prisma(f)
        elif f.language == "python" and ("op.create_table" in f.text or "op.add_column" in f.text):
            db.alembic(f)
    for f in files:
        if f.path in syntax:
            db.orm(f, syntax[f.path])
    for f in files:
        if f.path in syntax:
            db.usage(f, syntax[f.path])
    # ODM: a BOM class mapped to an execution class that maps to a table maps to that table too.
    class_tables: dict[str, list[str]] = {}
    for (etype, src, dst) in g.edges:
        if etype == "MAPS_TO" and dst.startswith(f"{g.repo}:db:"):
            class_tables.setdefault(src, []).append(dst)
    for edge in list(g.edges.values()):
        if edge.type == "MAPS_TO" and g.nodes.get(edge.src) and g.nodes[edge.src].labels[-1] == "BomClass":
            for tid in class_tables.get(edge.dst, ()):
                g.add_edge("MAPS_TO", edge.src, tid, edge.props["method"], min(edge.props["confidence"], 0.8))
