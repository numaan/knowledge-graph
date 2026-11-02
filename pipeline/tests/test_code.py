import os
import textwrap
from pathlib import Path

import pytest

from kg_pipeline.code.ingest import build_graph
from kg_pipeline.code.treesitter import parse

FILES = {
    "src/main/java/com/acme/loan/Borrower.java": """
        package com.acme.loan;
        public class Borrower extends Person implements Scored {
            private int age;
            public int getAge() { return age; }
            public boolean eligible() { return Rules.check(this) && getAge() >= 18; }
        }
    """,
    "src/main/java/com/acme/loan/Person.java": "package com.acme.loan;\npublic class Person {}\n",
    "src/main/java/com/acme/loan/Scored.java": "package com.acme.loan;\npublic interface Scored { int score(); }\n",
    "src/main/java/com/acme/loan/Rules.java": """
        package com.acme.loan;
        import com.acme.loan.Borrower;
        public final class Rules { static boolean check(Borrower b) { return true; } }
    """,
    "app/src/Service.kt": """
        package app
        interface Repo { fun load(): String }
        class Service(private val repo: Repo) : Repo {
            override fun load(): String = helper(repo.toString())
            fun helper(x: String) = x
        }
    """,
    "go.mod": "module example.com/shop\n\ngo 1.22\n",
    "cart/cart.go": """
        package cart
        type Cart struct{ items []string }
        func (c *Cart) Add(item string) { c.items = append(c.items, item); audit(item) }
        func audit(s string) {}
    """,
    "cmd/main.go": """
        package main
        import "example.com/shop/cart"
        func main() { c := &cart.Cart{}; c.Add("x") }
    """,
    "Billing/Invoice.cs": """
        using System;
        namespace Billing {
            interface ITotal { decimal Total(); }
            class Invoice : ITotal { public decimal Total() { return Sum(); } decimal Sum() => 0; }
        }
    """,
    "pkg/util.py": "def slug(s):\n    return s.lower()\n",
    "pkg/views.py": "from .util import slug\n\nclass View:\n    def title(self):\n        return slug('A')\n",
    "rules-project/rules/eligibility/minimumAge.brl": """<?xml version="1.0" encoding="UTF-8"?>
        <ilog.rules.studio.model.brl:ActionRule xmlns:ilog.rules.studio.model.brl="http://ilog.rules.studio/model/brl.ecore">
          <name>minimumAge</name>
          <definition><![CDATA[if the age of the borrower is less than 18 then reject the loan;]]></definition>
        </ilog.rules.studio.model.brl:ActionRule>
    """,
    "rules-project/rules/main.rfl": """<?xml version="1.0" encoding="UTF-8"?>
        <ilog.rules.studio.model.ruleflow:RuleFlow xmlns:ilog.rules.studio.model.ruleflow="http://ilog.rules.studio/model/ruleflow.ecore">
          <name>main</name>
          <body><Ruleflow><Body><TaskList><RuleTask Identifier="eligibility">
            <RuleList><Package Name="eligibility"/></RuleList></RuleTask></TaskList></Body></Ruleflow></body>
        </ilog.rules.studio.model.ruleflow:RuleFlow>
    """,
    "bom-project/bom/model.bom": """
        property loadGetterSetterAsProperties "true"
        package loan;

        public class Borrower
        {
            public int age;
            public boolean eligible();
        }
    """,
    "bom-project/bom/model_en_US.voc": "loan.Borrower#concept.label = borrower\nloan.Borrower.age#phrase.navigation = {age} of {this}\n",
    "bom-project/bom/model.b2x": """<?xml version="1.0" encoding="UTF-8"?>
        <b2x:translation xmlns:b2x="http://schemas.ilog.com/JRules/1.3/Translation">
          <class><businessName>loan.Borrower</businessName><executionName>com.acme.loan.Borrower</executionName></class>
        </b2x:translation>
    """,
}


@pytest.fixture(scope="module")
def graph(tmp_path_factory):
    repo = tmp_path_factory.mktemp("repo")
    for rel, body in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return build_graph(repo, "demo", scip_mode="off")


def edge(g, etype, src_suffix, dst_suffix):
    return next((e for e in g.edges.values() if e.type == etype and e.src.endswith(src_suffix)
                 and e.dst.endswith(dst_suffix)), None)


def test_java_supertypes_and_calls(graph):
    assert edge(graph, "EXTENDS", "Borrower.java#Borrower", "Person.java#Person")
    assert edge(graph, "IMPLEMENTS", "Borrower.java#Borrower", "Scored.java#Scored")
    same_file = edge(graph, "CALLS", "#Borrower.eligible", "#Borrower.getAge")
    assert same_file and same_file.props["confidence"] == 0.8
    assert edge(graph, "CALLS", "#Borrower.eligible", "Rules.java#Rules.check")
    assert edge(graph, "IMPORTS", "Rules.java", "Borrower.java")


def test_kotlin_interface_and_member_call(graph):
    assert graph.nodes["demo:app/src/Service.kt#Repo"].labels[-1] == "Interface"
    assert edge(graph, "IMPLEMENTS", "Service.kt#Service", "Service.kt#Repo")
    assert edge(graph, "CALLS", "#Service.load", "#Service.helper")
    # repo.toString() is a member call on another object with a generic name: no edge
    assert not any(e.dst.endswith("#Repo.load") and e.src.endswith("#Service.load") for e in graph.edges.values()
                   if e.type == "CALLS")


def test_go_methods_and_package_imports(graph):
    assert "demo:cart/cart.go#Cart.Add" in graph.nodes
    assert edge(graph, "CALLS", "cart.go#Cart.Add", "cart.go#audit")
    assert edge(graph, "IMPORTS", "cmd/main.go", "cart/cart.go")
    assert edge(graph, "CALLS", "main.go#main", "cart.go#Cart.Add")


def test_csharp(graph):
    assert edge(graph, "IMPLEMENTS", "Invoice.cs#Invoice", "Invoice.cs#ITotal")
    assert edge(graph, "CALLS", "#Invoice.Total", "#Invoice.Sum")


def test_python_relative_import_and_method_kind(graph):
    assert graph.nodes["demo:pkg/views.py#View.title"].labels[-1] == "Method"
    assert edge(graph, "IMPORTS", "pkg/views.py", "pkg/util.py")
    assert edge(graph, "CALLS", "views.py#View.title", "util.py#slug").props["confidence"] == 0.7


def test_odm_rules_bom_and_xom_mapping(graph):
    rule = "demo:odm:rules-project/rules/eligibility/minimumAge.brl#minimumAge"
    assert graph.nodes[rule].labels[-1] == "Rule"
    assert "less than 18" in graph.nodes[rule].props["text"]
    assert edge(graph, "IN_PACKAGE", "minimumAge", "odm:pkg:eligibility")
    assert edge(graph, "RUNS", "main.rfl#main", "odm:pkg:eligibility")
    assert edge(graph, "USES_TERM", "minimumAge", "odm:bom:loan.Borrower")
    mapping = edge(graph, "MAPS_TO", "odm:bom:loan.Borrower", "Borrower.java#Borrower")
    assert mapping and mapping.props["method"] == "b2x"


def test_every_parsed_line_is_in_a_chunk(graph):
    for f in [n for n in graph.nodes.values() if n.labels[-1] == "File" and n.props["language"] == "java"]:
        assert any(e.dst == f.id or e.dst.startswith(f.id + "#") for e in graph.edges.values() if e.type == "PART_OF")


def test_no_dangling_edges(graph):
    assert all(e.src in graph.nodes and e.dst in graph.nodes for e in graph.edges.values())


def test_syntax_errors_still_yield_symbols():
    result = parse("class A {\n  void ok() {}\n  void broken( {\n}\n", "java")
    assert result.has_errors
    assert any(s.qualname == "A.ok" for s in result.symbols)


@pytest.mark.skipif("NEO4J_PASSWORD" not in os.environ, reason="needs a Neo4j test database")
def test_neo4j_snapshot_removes_deleted_files(tmp_path):
    from kg_pipeline.graph.neo4j_writer import Neo4jWriter

    (tmp_path / "a.py").write_text("def a():\n    return b()\n")
    (tmp_path / "b.py").write_text("def b():\n    return 1\n")
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), "neo4j",
                     os.environ["NEO4J_PASSWORD"]) as w:
        first = w.write_snapshot(build_graph(tmp_path, "it-snapshot", "off"))
        (tmp_path / "b.py").unlink()
        second = w.write_snapshot(build_graph(tmp_path, "it-snapshot", "off"))
        left = w._run("MATCH (n:Code {repo: 'it-snapshot'}) RETURN count(n) AS c").records[0]["c"]
        w._run("MATCH (n:Code {repo: 'it-snapshot'}) DETACH DELETE n")
    assert first["stale_nodes_deleted"] == 0
    assert second["stale_nodes_deleted"] >= 3  # b.py, b(), its chunk
    assert left == second["nodes"]


@pytest.mark.skipif(not os.environ.get("NEO4J_PASSWORD"), reason="NEO4J_PASSWORD not set")
def test_delta_write_touches_only_changes(tmp_path):
    from kg_pipeline.graph.neo4j_writer import Neo4jWriter
    (tmp_path / "a.py").write_text("def a():\n    return b()\n")
    (tmp_path / "b.py").write_text("def b():\n    return 1\n")
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), "neo4j",
                     os.environ["NEO4J_PASSWORD"]) as w:
        try:
            first = w.write_delta(build_graph(tmp_path, "it-delta", "off"))
            w._run("MATCH (n:Code {repo: 'it-delta', name: 'a'}) SET n.embedding = [1.0]")
            again = w.write_delta(build_graph(tmp_path, "it-delta", "off"))
            (tmp_path / "b.py").write_text("def b():\n    return 2\n")
            changed = w.write_delta(build_graph(tmp_path, "it-delta", "off"))
            (tmp_path / "b.py").unlink()
            removed = w.write_delta(build_graph(tmp_path, "it-delta", "off"))
            kept = w._run("MATCH (n:Code {repo: 'it-delta', name: 'a'}) RETURN n.embedding AS e").records[0]["e"]
            left = w._run("MATCH (n:Code {repo: 'it-delta'}) RETURN count(n) AS c").records[0]["c"]
        finally:
            w._run("MATCH (n:Code {repo: 'it-delta'}) DETACH DELETE n")
    assert first["nodes_written"] == first["nodes"]
    assert again["nodes_written"] == 0 and again["edges_written"] == 0      # nothing changed
    assert 0 < changed["nodes_written"] <= 4                                 # b.py, b(), its chunk, repo
    assert removed["stale_nodes_deleted"] >= 3 and left == removed["nodes"]
    assert kept == [1.0]                                                     # untouched node keeps its embedding
