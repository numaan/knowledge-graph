// Mock of the kg-mcp REST layer (mcp/rest.go) for the Playwright smoke test and for screenshots.
//   node e2e/mock-api.mjs [port]        MOCK_NO_ASK=1 removes the ask tool (404, "not configured")
import { createServer } from "node:http";
import { handleControl } from "./mock-control.mjs";

const port = Number(process.argv[2] ?? process.env.MOCK_PORT ?? 8799);
const noAsk = process.env.MOCK_NO_ASK === "1";
const feedback = [];

const SYM = "precept:backend/app/routers/users.py#bulk_invite";
const VIEW = "precept:frontend/src/views/UsersView.tsx#UsersView.onBulkInvite";
const FILE = "precept:backend/app/routers/users.py";
const TICKET = "jira:PREC-142";
const SECTION = "docs:precept-docs:user-management.md#s2";
const REQ = "req:precept:FR-P04-02-01";
const PERSON = "person:ada";

const nodes = {
  [SYM]: { labels: ["Code", "Symbol"], properties: { id: SYM, qualname: "bulk_invite", name: "bulk_invite", kind: "Function",
    path: "backend/app/routers/users.py", start_line: 88, end_line: 101, repo: "precept", role: "endpoint",
    http_method: "POST", route: "/orgs/{org_id}/invitations/bulk", signature: "async def bulk_invite(org_id: str, body: BulkInvite, user=Depends(admin))" } },
  [VIEW]: { labels: ["Code", "Symbol"], properties: { id: VIEW, qualname: "UsersView.onBulkInvite", kind: "Method",
    path: "frontend/src/views/UsersView.tsx", start_line: 40, repo: "precept" } },
  [FILE]: { labels: ["Code", "File"], properties: { id: FILE, path: "backend/app/routers/users.py", repo: "precept", language: "python" } },
  [TICKET]: { labels: ["Source", "Ticket"], properties: { id: TICKET, key: "PREC-142", title: "Bulk invite users from a CSV file",
    status: "Done", type: "Story", priority: "High", url: "https://example.atlassian.net/browse/PREC-142",
    text: "As an org admin I want to invite many people at once, so that onboarding a team takes minutes.\n\nAcceptance criteria:\n- Upload up to 500 e-mail addresses\n- Skip addresses that already have an account\n- Show a summary of sent, skipped and failed invitations", stale: true } },
  [SECTION]: { labels: ["Source", "Section"], properties: { id: SECTION, title: "User management > Invitations",
    text: "Admins invite users by e-mail. An invitation expires after 7 days. Bulk invitations accept a CSV file with one address per line; the server de-duplicates addresses and skips existing members.", conflict: true } },
  [REQ]: { labels: ["Requirement"], properties: { id: REQ, ref: "FR-P04-02-01", statement: "An admin can invite up to 500 users in one request.",
    verdict: "supported", confidence: 0.86, repo: "precept", source: "docs/features/P04-02-user-management.md" } },
  [PERSON]: { labels: ["Source", "Person"], properties: { id: PERSON, name: "Ada Lovelace", github: "ada", names: ["Ada Lovelace", "ada"] } },
};

const neighbors = {
  [SYM]: [
    { id: FILE, labels: ["Code", "File"], name: "backend/app/routers/users.py", distance: 1, via: ["DEFINES"] },
    { id: VIEW, labels: ["Code", "Symbol"], name: "UsersView.onBulkInvite", distance: 1, via: ["CALLS"] },
    { id: REQ, labels: ["Requirement"], name: "An admin can invite up to 500 users in one request.", distance: 1, via: ["IMPLEMENTED_BY"] },
    { id: TICKET, labels: ["Source", "Ticket"], name: "Bulk invite users from a CSV file", distance: 1, via: ["MENTIONS"] },
    { id: "precept:backend/app/services/invitations.py#create_invitation", labels: ["Code", "Symbol"], name: "create_invitation", distance: 1, via: ["CALLS"] },
    { id: "domain:precept:Invitation", labels: ["Domain", "DomainConcept"], name: "Invitation", distance: 1, via: ["REALISES"] },
    { id: PERSON, labels: ["Source", "Person"], name: "Ada Lovelace", distance: 1, via: ["AUTHORED"] },
  ],
  [TICKET]: [
    { id: SYM, labels: ["Code", "Symbol"], name: "bulk_invite", distance: 1, via: ["MENTIONS"] },
    { id: REQ, labels: ["Requirement"], name: "An admin can invite up to 500 users in one request.", distance: 1, via: ["SPECIFIES"] },
  ],
};

const code = `async def bulk_invite(org_id: str, body: BulkInvite, user=Depends(admin)):
    """Invite many people to an org at once."""
    if len(body.emails) > 500:
        raise HTTPException(413, "at most 500 addresses")
    sent, skipped = [], []
    for email in dict.fromkeys(e.strip().lower() for e in body.emails):
        if await members.exists(org_id, email):
            skipped.append(email)
            continue
        sent.append(await create_invitation(org_id, email, invited_by=user.id))
    return {"sent": len(sent), "skipped": skipped}`;

const tools = {
  search_graph: () => ({ results: [
    { path: "backend/app/routers/users.py", start_line: 88, end_line: 101, symbol_id: SYM, symbol: "bulk_invite", kind: "Function",
      role: "endpoint", route: "/orgs/{org_id}/invitations/bulk", concepts: ["Invitation", "Organisation"], code: code.split("\n").slice(0, 6).join("\n"), score: 0.031 },
    { path: "frontend/src/views/UsersView.tsx", start_line: 40, symbol_id: VIEW, symbol: "UsersView.onBulkInvite", kind: "Method",
      concepts: ["Invitation"], code: "const onBulkInvite = async (file: File) => {\n  const emails = parseCsv(await file.text());", score: 0.029 },
  ] }),
  search_knowledge: (a) => ({ results: [
    { id: TICKET, kind: "Ticket", key: "PREC-142", type: "Story", status: "Done", title: "Bulk invite users from a CSV file",
      url: "https://example.atlassian.net/browse/PREC-142", text: nodes[TICKET].properties.text },
    { id: SECTION, kind: "Section", title: "User management > Invitations", text: nodes[SECTION].properties.text },
  ].filter((r) => !a.kind || r.kind === a.kind) }),
  ask: (a) => ({ data: {
    answer: `Admins invite users in bulk through POST /orgs/{org_id}/invitations/bulk, handled by bulk_invite [1]. ` +
      `The frontend calls it from UsersView.onBulkInvite after it parses the CSV file [2].\n\n` +
      `The limit is 500 addresses per request, and existing members are skipped [1][3]. (Question: ${a.question})`,
    citations: [
      { id: SYM, title: "bulk_invite (backend/app/routers/users.py)", kind: "Symbol" },
      { id: VIEW, title: "UsersView.onBulkInvite", kind: "Symbol" },
      { id: TICKET, title: "PREC-142: Bulk invite users from a CSV file", kind: "Ticket", url: "https://example.atlassian.net/browse/PREC-142" },
    ],
    confidence: 0.82,
  } }),
  get_symbol_code: () => ({ data: { id: SYM, qualname: "bulk_invite", path: "backend/app/routers/users.py", start_line: 88,
    end_line: 101, signature: nodes[SYM].properties.signature, code } }),
  get_change_history: () => ({ results: [
    { sha: "3f2a9c1e7b", date: "2026-10-02T10:14:00Z", author: "Ada Lovelace", title: "Skip existing members in bulk invite", tickets: ["PREC-142: Bulk invite users from a CSV file"] },
    { sha: "91be0d44aa", date: "2026-09-18T16:40:00Z", author: "Grace Hopper", title: "Add bulk invitation endpoint", tickets: [] },
  ] }),
  get_neighbors: (a) => ({ results: neighbors[a.node_id] ?? [] }),
  trace_requirement: () => ({ data: { id: REQ, ref: "FR-P04-02-01", verdict: "supported", confidence: 0.86,
    reasoning: "bulk_invite rejects more than 500 addresses and invites each remaining address.", gaps: [],
    evidence: [{ link: "IMPLEMENTED_BY", symbol_id: SYM, symbol: "bulk_invite", path: "backend/app/routers/users.py", explanation: "checks len(body.emails) > 500" }] } }),
  get_context_for_ticket: () => ({ data: { ticket: nodes[TICKET].properties, links: [],
    commits: [{ sha: "3f2a9c1e7b", title: "Skip existing members in bulk invite", author: "Ada Lovelace", repo: "precept" }] } }),
  domain_overview: (a) => a.repo ? ({ data: { repo: a.repo,
    capabilities: [{ module: "backend/app/routers", name: "User management", summary: "Invite, list and remove members of an organisation." },
      { module: "backend/app/scenarios", name: "Scenario builder", summary: "Compose fault scenarios and run them against a target." }],
    concepts: [{ name: "Invitation", kind: "entity", definition: "An offer to join an organisation, valid for 7 days.", symbols: 12 },
      { name: "Scenario", kind: "entity", definition: "An ordered list of faults to inject.", symbols: 31 }],
    rules: [{ module: "backend/app/routers", statement: "An invitation expires after 7 days." },
      { module: "backend/app/routers", statement: "Only admins can invite users." }],
    entities: [{ entity: "Invitation", path: "backend/app/models/invitation.py" }],
    endpoints: [{ method: "POST", route: "/orgs/{org_id}/invitations/bulk", handler: "bulk_invite", path: "backend/app/routers/users.py" }],
  } }) : ({ data: { repositories: [{ repo: "precept", commit: "5dc418c", files: 412 }] } }),
};

const requirements = [
  { id: REQ, ref: "FR-P04-02-01", statement: "An admin can invite up to 500 users in one request.", source: "docs/features/P04-02-user-management.md",
    repo: "precept", verdict: "supported", confidence: 0.86, gaps: [], tests: 2,
    evidence: [{ link: "IMPLEMENTED_BY", id: SYM, symbol: "bulk_invite", path: "backend/app/routers/users.py", confidence: 0.86 }] },
  { id: "req:precept:FR-P04-02-02", ref: "FR-P04-02-02", statement: "Invitations expire after 72 hours.", source: "docs/features/P04-02-user-management.md",
    repo: "precept", verdict: "contradicted", confidence: 0.78, gaps: ["code uses 7 days"], tests: 0,
    evidence: [{ link: "CONTRADICTED_BY", id: "precept:backend/app/services/invitations.py#create_invitation", symbol: "create_invitation", path: "backend/app/services/invitations.py" }] },
  { id: "req:precept:FR-P10-03-02", ref: "FR-P10-03-02", statement: "A scenario can be exported as JSON.", source: "docs/features/P10-03-scenario-builder.md",
    repo: "precept", verdict: "unresolved", confidence: 0.4, gaps: ["no export found"], tests: 0, evidence: [] },
];

function send(res, status, body) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");
  let body = "";
  for await (const c of req) body += c;
  const args = body ? JSON.parse(body) : {};
  const p = url.pathname;
  const control = handleControl(req.method, p, args);
  if (control) {
    if (control.body === null) {
      res.writeHead(control.status);
      return res.end();
    }
    return send(res, control.status, control.body);
  }
  if (req.method === "GET" && p === "/api/tools") {
    return send(res, 200, { tools: Object.keys(tools).filter((t) => !(noAsk && t === "ask")).map((name) => ({ name, description: name, input_schema: { type: "object" } })) });
  }
  if (req.method === "POST" && p.startsWith("/api/tools/")) {
    const name = decodeURIComponent(p.slice("/api/tools/".length));
    if (!tools[name] || (noAsk && name === "ask")) return send(res, 404, { error: `tool "${name}" is not configured on this server` });
    return send(res, 200, tools[name](args));
  }
  if (req.method === "GET" && p.startsWith("/api/node/")) {
    const id = decodeURIComponent(p.slice("/api/node/".length));
    if (!nodes[id]) return send(res, 404, { error: `node "${id}" not found or not visible` });
    return send(res, 200, { node: nodes[id], neighbors: { results: neighbors[id] ?? [] } });
  }
  if (req.method === "GET" && p === "/api/requirements") {
    const counts = { supported: 1, contradicted: 1, unresolved: 1 };
    return send(res, 200, { counts, total: requirements.length, requirements });
  }
  if (req.method === "POST" && p === "/api/feedback") {
    if (!["wrong", "outdated", "helpful", "missing"].includes(args.kind) || !args.node_id) return send(res, 400, { error: "invalid" });
    feedback.push(args);
    return send(res, 202, { status: "accepted" });
  }
  if (p === "/__feedback") return send(res, 200, feedback);
  send(res, 404, { error: "not found" });
}).listen(port, () => console.log(`mock kg API on :${port}`));
