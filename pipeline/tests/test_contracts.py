import os
import shutil
import textwrap
from pathlib import Path

import pytest

from kg_pipeline.code import links
from kg_pipeline.code.httpcalls import client_calls, norm_route, topics
from kg_pipeline.code.ingest import build_graph

SERVICES = Path(__file__).parent / "fixtures" / "services"


def _copy(tmp: Path, name: str, rename: dict[str, str] | None = None) -> Path:
    """Copy a fixture service out of the git checkout (build_graph then walks the folder)."""
    dst = tmp / name
    shutil.copytree(SERVICES / name, dst)
    for old, new in (rename or {}).items():
        cfg = dst / "kg-links.yaml"
        cfg.write_text(cfg.read_text().replace(old, new))
    return dst


@pytest.fixture(scope="module")
def graphs(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("services")
    return {n: build_graph(_copy(tmp, n), n, "off") for n in ("orders-service", "billing-service")}


@pytest.fixture(scope="module")
def linked(graphs):
    edges, topic_nodes = links.resolve(*links.facts_from_graphs(list(graphs.values())))
    return {(e["type"], e["src"], e["dst"]): e for e in edges}, topic_nodes


def _edges(g, etype):
    return {(e.src, e.dst): e.props for e in g.edges.values() if e.type == etype}


def test_openapi_operations_and_schemas(graphs):
    g = graphs["orders-service"]
    op = g.nodes["orders-service:openapi.yaml#POST /api/orders"]
    assert op.labels == ("Code", "Contract", "ApiOperation")
    assert op.props["operation_id"] == "createOrder" and op.props["path_template"] == "/api/orders"
    assert op.props["request_schemas"] == ["NewOrder"] and op.props["response_schemas"] == ["Order"]
    get = g.nodes["orders-service:openapi.yaml#GET /api/orders/{orderId}"]
    assert get.props["route_key"] == "/api/orders/{}"
    uses = _edges(g, "USES_SCHEMA")
    assert uses[("orders-service:openapi.yaml#POST /api/orders", "orders-service:openapi.yaml#schema:NewOrder")]["role"] == "request"
    assert ("orders-service:openapi.yaml#schema:Order", "orders-service:openapi.yaml#schema:OrderLine") in uses
    assert g.nodes["orders-service:openapi.yaml#schema:Order"].props["properties"] == ["id", "lines", "status"]


def test_asyncapi_operation(graphs):
    op = graphs["billing-service"].nodes["billing-service:asyncapi.yaml#SUBSCRIBE order-created"]
    assert op.props["spec"] == "asyncapi" and op.props["channel"] == "order-created"
    assert op.props["operation_id"] == "onOrderCreated"


def test_tables_columns_and_migrations(graphs):
    g = graphs["orders-service"]
    cols = {n.props["name"] for n in g.nodes.values() if n.labels[-1] == "Column" and n.props["table"] == "orders"}
    assert cols == {"id", "customer_id", "status"}            # status added by 002, created_at dropped
    assert g.nodes["orders-service:db:orders"].props["sources"] == ["sql", "sqlalchemy"]
    assert ("orders-service:db:order_lines", "orders-service:db:orders") in _edges(g, "FOREIGN_KEY")
    assert ("orders-service:migrations/002_status.sql", "orders-service:db:orders") in _edges(g, "MIGRATES")
    assert _edges(g, "MAPS_TO")[("orders-service:app/models.py#Order", "orders-service:db:orders")]["confidence"] == 0.9
    assert ("orders-service:app/repository.py#load_order", "orders-service:db:orders") in _edges(g, "READS")
    assert ("orders-service:app/repository.py#add_line", "orders-service:db:order_lines") in _edges(g, "WRITES")


def test_prisma_schema_and_client_usage(graphs):
    g = graphs["billing-service"]
    cols = {n.props["name"]: n.props for n in g.nodes.values() if n.labels[-1] == "Column"}
    assert set(cols) == {"id", "order_id", "total", "paid"} and cols["id"]["primary_key"] and cols["paid"]["nullable"]
    assert ("billing-service:src/routes.ts#createInvoice", "billing-service:db:invoices") in _edges(g, "WRITES")
    assert ("billing-service:src/routes.ts#getInvoice", "billing-service:db:invoices") in _edges(g, "READS")


def test_endpoint_routes_with_prefixes(graphs):
    o, b = graphs["orders-service"].nodes, graphs["billing-service"].nodes
    # FastAPI: APIRouter(prefix="/orders") included in APIRouter(prefix="/api")
    assert o["orders-service:app/routers/orders.py#create_order"].props["endpoint_route"] == "/api/orders"
    assert o["orders-service:app/routers/orders.py#get_order"].props["endpoint_method"] == "GET"
    # Express: named handler and inline handler (a Route symbol)
    assert b["billing-service:src/routes.ts#createInvoice"].props["endpoint_route"] == "/invoices"
    assert b["billing-service:src/routes.ts#route:GET /health"].labels[-1] == "Route"


def test_links_between_the_two_services(linked):
    edges, topic_nodes = linked
    call = edges[("CALLS_HTTP", "orders-service:app/billing.py#create_invoice", "billing-service:src/routes.ts#createInvoice")]
    assert call["method"] == "heuristic" and call["confidence"] == 0.95 and call["cross_repo"]
    back = edges[("CALLS_HTTP", "billing-service:src/orders.ts#fetchOrder", "orders-service:app/routers/orders.py#get_order")]
    assert back["match"] == "exact" and back["cross_repo"]
    assert ("IMPLEMENTS", "orders-service:app/routers/orders.py#create_order", "orders-service:openapi.yaml#POST /api/orders") in edges
    assert not any(k[2].endswith("/cancel") for k in edges)       # the spec-only operation has no implementer
    tests = {(k[1], k[2]) for k in edges if k[0] == "TESTS"}
    assert ("orders-service:tests/test_orders.py#test_create_order", "orders-service:app/routers/orders.py#create_order") in tests
    assert ("orders-service:tests/test_orders.py#test_get_order", "orders-service:app/routers/orders.py#get_order") in tests
    assert ("billing-service:test/invoices.test.ts", "billing-service:src/routes.ts#createInvoice") in tests
    assert ("PUBLISHES", "orders-service:app/events.py#publish_order_created", "topic:order-created") in edges
    assert ("SUBSCRIBES", "billing-service:src/consumer.ts#startConsumer", "topic:order-created") in edges
    assert ("DESCRIBES", "billing-service:asyncapi.yaml#SUBSCRIBE order-created", "topic:order-created") in edges
    assert topic_nodes["topic:order-created"]["repos"] == ["billing-service", "orders-service"]
    assert all(e["method"] == "heuristic" and e["layer"] == "links" for e in edges.values())


def test_route_match_rules():
    seg = lambda p: [s for s in norm_route(p).split("/") if s]
    assert links.match(seg("/api/orders/{id}"), seg("/api/orders/:orderId")) == (0.9, "exact")
    assert links.match(seg("/api/orders/42"), seg("/api/orders/{id}")) == (0.8, "param")
    assert links.match(seg("/orders/42"), seg("/api/v1/orders/{id}")) == (0.6, "suffix")
    assert links.match(seg("/orders"), seg("/api/orders")) is None                # one segment is too weak
    assert links.match(seg("/api/orders"), seg("/api/invoices")) is None
    svc = links.merge_config({"services": {"billing": {"repo": "b", "base_path": "/api"}}})
    assert links._service("BILLING_SERVICE_URL", svc)["repo"] == "b" and links._service("ORDERS_URL", svc) is None


def test_ambiguous_routes_are_not_linked():
    eps = [{"id": f"r{i}:x#h", "repo": f"r{i}", "path": "x.py", "endpoint_method": "GET", "endpoint_route": "/health"}
           for i in range(5)]
    caller = {"id": "c:y#f", "repo": "c", "path": "y.py", "http_calls": ["GET /health"]}
    edges, _ = links.resolve(eps + [caller], [], {})
    assert edges == []
    edges, _ = links.resolve(eps[:2] + [caller], [], {})
    assert len(edges) == 2 and all(e["confidence"] == 0.72 for e in edges)     # 0.9 x 0.8 for a tie


@pytest.mark.parametrize("code,expected", [
    ('const r = await fetch(`${API_BASE}/api/auth/login`, { method: "POST" });', ("POST", "/api/auth/login", "API_BASE")),
    ("await axios.delete(`/api/orders/${id}`)", ("DELETE", "/api/orders/${id}", None)),
    ("request(app).post('/invoices').send({})", ("POST", "/invoices", None)),
    ('r = client.post("/api/auth/login", headers=h)', ("POST", "/api/auth/login", None)),
    ('requests.get(f"{BASE}/orders/{oid}")', ("GET", "/orders/{oid}", "BASE")),
    ("requests.request('PUT', BASE + '/orders/' + oid)", ("PUT", "/orders/{}", "BASE")),
    ('req := httptest.NewRequest("GET", "/orders/42", nil)', ("GET", "/orders/42", None)),
    ('http.NewRequestWithContext(ctx, http.MethodPost, "http://billing:8080/invoices", body)', ("POST", "/invoices", "billing")),
    ('restTemplate.getForObject("http://orders/api/orders/{id}", Order.class, id);', ("GET", "/api/orders/{id}", "orders")),
    ('mockMvc.perform(post("/api/orders"))', ("POST", "/api/orders", None)),
    ('webClient.get().uri("/api/x").retrieve()', ("GET", "/api/x", None)),
    ('restTemplate.exchange("/api/y", HttpMethod.PUT, entity, X.class)', ("PUT", "/api/y", None)),
    ('await client.GetAsync("/api/orders/1");', ("GET", "/api/orders/1", None)),
    ('new HttpRequestMessage(HttpMethod.Delete, "/api/orders/1")', ("DELETE", "/api/orders/1", None)),
])
def test_http_client_patterns(code, expected):
    assert [c[1:] for c in client_calls(code)] == [expected]


@pytest.mark.parametrize("code", [
    '@router.get("/x")', '@GetMapping("/x")', '[HttpGet("/x")]', "router.post('/invoices', createInvoice)",
    'x = d.get("key")', 'os.environ.get("BASE_URL")', "fetch(url)",
])
def test_not_client_calls(code):
    assert client_calls(code) == []


def test_topic_patterns():
    found = {(d, b, n) for _, d, b, n in topics(textwrap.dedent("""
        producer.send("order-created", v)
        await producer.send({ topic: "invoice-paid", messages })
        await consumer.subscribe({ topic: "order-created" })
        kafkaTemplate.send("audit", x);
        channel.basic_publish(exchange="", routing_key="jobs", body=b)
        channel.basic_consume(queue="jobs", on_message_callback=cb)
        sqs.send_message(QueueUrl="https://sqs.eu-west-1.amazonaws.com/123/emails", MessageBody=m)
        nc.publish("orders.created", b)
        await nc.subscribe("orders.*")
    """))}
    assert found == {("publishes", "kafka", "order-created"), ("publishes", "kafka", "invoice-paid"),
                     ("subscribes", "kafka", "order-created"), ("publishes", "kafka", "audit"),
                     ("publishes", "rabbitmq", "jobs"), ("subscribes", "rabbitmq", "jobs"),
                     ("publishes", "sqs", "emails"), ("publishes", "nats", "orders.created"),
                     ("subscribes", "nats", "orders.*")}


def test_annotated_routes_and_models_in_other_languages(tmp_path):
    files = {
        "src/main/java/a/OrderController.java": """
            package a;
            @RestController
            @RequestMapping("/api/orders")
            public class OrderController {
                @GetMapping("/{id}") public Order get(long id) { return null; }
                @RequestMapping(value = "/{id}/cancel", method = RequestMethod.POST) public void cancel(long id) {}
                @KafkaListener(topics = "order-created") public void on(String m) {}
            }
        """,
        "src/main/java/a/Order.java": """
            package a;
            @Entity
            @Table(name = "orders")
            public class Order { @Column(name = "total_cents") private long total; }
        """,
        "src/main/java/a/OrderRepository.java": """
            package a;
            public interface OrderRepository extends JpaRepository<Order, Long> {}
        """,
        "api/Controllers/InvoicesController.cs": """
            [ApiController]
            [Route("api/[controller]")]
            public class InvoicesController : ControllerBase {
                [HttpPost("{id}/pay")] public IActionResult Pay(int id) { return Ok(); }
            }
        """,
        "web/auth.controller.ts": """
            @Controller('auth')
            export class AuthController { @Post('login') login() {} }
        """,
        "shop/models.py": """
            from django.db import models
            class Product(models.Model):
                name = models.CharField(max_length=40)
                shop = models.ForeignKey("Shop", on_delete=models.CASCADE)
            def cheap():
                return Product.objects.filter(name="x")
        """,
        "alembic/versions/001_users.py": """
            def upgrade():
                op.create_table("users", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("email", sa.String()))
                op.add_column("users", sa.Column("name", sa.String()))
        """,
        "cmd/server.go": """
            package main
            func listOrders(w http.ResponseWriter, r *http.Request) {}
            func main() { http.HandleFunc("GET /orders", listOrders) }
        """,
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(textwrap.dedent(text))
    g = build_graph(tmp_path, "poly", "off")
    route = lambda sid: (g.nodes[sid].props.get("endpoint_method"), g.nodes[sid].props.get("endpoint_route"))
    assert route("poly:src/main/java/a/OrderController.java#OrderController.get") == ("GET", "/api/orders/{id}")
    assert route("poly:src/main/java/a/OrderController.java#OrderController.cancel") == ("POST", "/api/orders/{id}/cancel")
    assert route("poly:api/Controllers/InvoicesController.cs#InvoicesController.Pay") == ("POST", "/api/Invoices/{id}/pay")
    assert route("poly:web/auth.controller.ts#AuthController.login") == ("POST", "/auth/login")
    assert route("poly:cmd/server.go#listOrders") == ("GET", "/orders")
    assert g.nodes["poly:src/main/java/a/OrderController.java#OrderController.on"].props["subscribes"] == ["kafka:order-created"]
    assert ("poly:src/main/java/a/Order.java#Order", "poly:db:orders") in _edges(g, "MAPS_TO")
    assert "poly:db:orders.total_cents" in g.nodes
    assert ("poly:src/main/java/a/OrderRepository.java#OrderRepository", "poly:db:orders") in _edges(g, "WRITES")
    assert _edges(g, "MAPS_TO")[("poly:shop/models.py#Product", "poly:db:shop_product")]["confidence"] == 0.7
    assert "poly:db:shop_product.shop_id" in g.nodes
    assert ("poly:shop/models.py#cheap", "poly:db:shop_product") in _edges(g, "READS")
    assert {"poly:db:users.id", "poly:db:users.email", "poly:db:users.name"} <= set(g.nodes)


@pytest.mark.skipif(not os.environ.get("KG_PRECEPT_REPO"), reason="KG_PRECEPT_REPO not set (read-only precept checkout)")
def test_precept_login_test_reaches_login_endpoint():
    g = build_graph(Path(os.environ["KG_PRECEPT_REPO"]), "precept", "off")
    edges, _ = links.resolve(*links.facts_from_graphs([g]))
    login = "precept:backend/app/routers/auth.py#login"
    tests = {e["src"] for e in edges if e["type"] == "TESTS" and e["dst"] == login}
    assert "precept:backend/tests/test_audit.py#test_login_sets_last_login_and_records_event" in tests
    assert any(e["type"] == "CALLS_HTTP" and e["dst"] == login and "frontend" in e["src"] for e in edges)


@pytest.mark.skipif(not os.environ.get("NEO4J_PASSWORD"), reason="NEO4J_PASSWORD not set")
def test_neo4j_links_sync_is_incremental(tmp_path):
    from kg_pipeline.graph.neo4j_writer import Neo4jWriter

    names = {"orders-service": "it-orders", "billing-service": "it-billing"}
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        try:
            graphs = {n: build_graph(_copy(tmp_path, n, names), it, "off") for n, it in names.items()}
            for g in graphs.values():
                w.write_delta(g)
            first = links.sync(w)
            assert first["edges"]["CALLS_HTTP"] >= 2 and first["topics"] >= 1
            again = links.sync(w)
            assert again["edges_written"] == 0 and again["edges_deleted"] == 0
            # A code snapshot of one repo must keep the link edges (they belong to the links step).
            w.write_delta(graphs["orders-service"])
            rows = w._run("MATCH (:Code {repo: 'it-orders'})-[r:CALLS_HTTP]->(:Code {repo: 'it-billing'}) "
                          "RETURN r.confidence AS c").records
            assert rows and rows[0]["c"] == 0.95
        finally:
            w._run("MATCH (n:Code) WHERE n.repo IN ['it-orders', 'it-billing'] DETACH DELETE n")
            links.sync(w)   # removes topics without edges
