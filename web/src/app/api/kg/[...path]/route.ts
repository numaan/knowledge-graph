import { kgFetch } from "@/lib/kg";
import { upstreamPath } from "@/lib/proxy-path";

// Browser-facing proxy to the kg-mcp REST layer. It adds the caller's access token on the server,
// so the token never reaches the browser, and forwards only the REST routes the UI needs.

async function forward(req: Request, path: string[], method: "GET" | "POST") {
  const upstream = upstreamPath(path);
  if (upstream === null) return Response.json({ error: "not found" }, { status: 404 });
  const search = new URL(req.url).search;
  const body = method === "POST" ? await req.text() : undefined;
  if (body && body.length > 1 << 20) return Response.json({ error: "body too large" }, { status: 413 });
  const res = await kgFetch<unknown>(upstream + search, { method, body: body || (method === "POST" ? "{}" : undefined) });
  if (!res.ok) return Response.json({ error: res.error }, { status: res.status });
  return Response.json(res.data, { status: method === "POST" && path[0] === "feedback" ? 202 : 200 });
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(req: Request, { params }: Ctx) {
  return forward(req, (await params).path, "GET");
}

export async function POST(req: Request, { params }: Ctx) {
  return forward(req, (await params).path, "POST");
}
