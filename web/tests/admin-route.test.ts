import { afterEach, beforeEach } from "vitest";

vi.mock("server-only", () => ({}));
vi.mock("@/auth", () => ({ accessToken: vi.fn(async () => "SECRET-TOKEN") }));

import { accessToken } from "@/auth";
import { DELETE, GET, PATCH, POST } from "@/app/api/admin/[...path]/route";
import { controlFetch } from "@/lib/control";

const ORIGIN = "http://localhost:3000";
const ctx = (...path: string[]) => ({ params: Promise.resolve({ path }) });
const req = (method: string, path: string, init: { headers?: Record<string, string>; body?: BodyInit | null } = {}) =>
  new Request(`${ORIGIN}/api/admin/${path}`, { method, ...init, headers: init.headers });
const good = { "x-kg-admin": "1", origin: ORIGIN, "content-type": "application/json" };

let fetchMock: ReturnType<typeof vi.fn>;
const upstream = (body: unknown, status = 200, extra: Record<string, string> = {}) =>
  vi.fn(async (_u: string | URL | Request, _i?: RequestInit) =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", "set-cookie": "a=b", "x-secret": "1", ...extra } }),
  );

beforeEach(() => {
  process.env.CONTROL_API_URL = "http://control:8090/";
  vi.mocked(accessToken).mockResolvedValue("SECRET-TOKEN");
  fetchMock = upstream({ sources: [] });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.CONTROL_API_URL;
});

describe("/api/admin proxy", () => {
  it("forwards an allowed path with the server-side token and hides it from the response", async () => {
    const res = await GET(req("GET", "sources?x=1", { headers: { authorization: "Bearer BROWSER", cookie: "s=1" } }), ctx("sources"));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ sources: [] });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("http://control:8090/api/v1/sources?x=1");
    const h = new Headers(init?.headers);
    expect(h.get("authorization")).toBe("Bearer SECRET-TOKEN");
    expect(h.get("cookie")).toBeNull();
    expect(JSON.stringify([...res.headers])).not.toContain("SECRET-TOKEN");
    expect([...res.headers.keys()].filter((k) => k !== "content-type")).toEqual([]);
  });

  it("forwards a POST body with CSRF headers", async () => {
    fetchMock = upstream({ id: "a" }, 201);
    vi.stubGlobal("fetch", fetchMock);
    const res = await POST(req("POST", "sources", { headers: good, body: '{"name":"a"}' }), ctx("sources"));
    expect(res.status).toBe(201);
    expect(fetchMock.mock.calls[0][1]?.method).toBe("POST");
    expect(String(fetchMock.mock.calls[0][1]?.body)).toContain('"name":"a"');
  });

  it("forwards PATCH and DELETE", async () => {
    expect((await PATCH(req("PATCH", "sources/a", { headers: good, body: "{}" }), ctx("sources", "a"))).status).toBe(200);
    expect((await DELETE(req("DELETE", "sources/a", { headers: good }), ctx("sources", "a"))).status).toBe(200);
    expect(fetchMock.mock.calls.map((c) => c[1]?.method)).toEqual(["PATCH", "DELETE"]);
  });

  it.each([["sources", "..", "x"], ["%2E%2E"], ["sources", "a%2Fb"], ["secrets"], ["sources", "a", "b", "c"]])("returns 404 for %j", async (...p) => {
    const res = await GET(req("GET", "x"), ctx(...p));
    expect(res.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns 405 for a method that the path does not allow", async () => {
    const res = await DELETE(req("DELETE", "runs/1", { headers: good }), ctx("runs", "1"));
    expect(res.status).toBe(405);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    ["no custom header", { origin: ORIGIN }],
    ["wrong custom header", { "x-kg-admin": "0", origin: ORIGIN }],
    ["foreign origin", { "x-kg-admin": "1", origin: "http://evil.example" }],
    ["no origin and no fetch metadata", { "x-kg-admin": "1" }],
    ["cross-site fetch metadata", { "x-kg-admin": "1", "sec-fetch-site": "cross-site" }],
  ])("returns 403 for POST with %s", async (_n, headers) => {
    const res = await POST(req("POST", "sources", { headers, body: "{}" }), ctx("sources"));
    expect(res.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns 403 for PATCH and DELETE without CSRF", async () => {
    expect((await PATCH(req("PATCH", "sources/a", { body: "{}" }), ctx("sources", "a"))).status).toBe(403);
    expect((await DELETE(req("DELETE", "sources/a"), ctx("sources", "a"))).status).toBe(403);
  });

  it("accepts same-origin fetch metadata without Origin", async () => {
    const res = await POST(req("POST", "sources/a/run", { headers: { "x-kg-admin": "1", "sec-fetch-site": "same-origin" }, body: "{}" }), ctx("sources", "a", "run"));
    expect(res.status).toBe(200);
  });

  it("accepts the public host from the Host header although req.url holds the bind address", async () => {
    const headers = { ...good, host: "kg.example.com", origin: "https://kg.example.com", "x-forwarded-proto": "https" };
    const res = await POST(req("POST", "sources", { headers, body: "{}" }), ctx("sources"));
    expect(res.status).toBe(200);
  });

  it("passes a 204 (delete source) through without a body", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 204 })));
    const res = await DELETE(req("DELETE", "sources/a", { headers: good }), ctx("sources", "a"));
    expect(res.status).toBe(204);
    expect(await res.text()).toBe("");
  });

  it("returns 413 for a body over 1 MiB", async () => {
    const big = "x".repeat(1024 * 1024 + 1);
    const res = await POST(req("POST", "sources", { headers: good, body: big }), ctx("sources"));
    expect(res.status).toBe(413);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("passes upstream 4xx and 5xx status and JSON body through", async () => {
    fetchMock = upstream({ error: "bad name" }, 400);
    vi.stubGlobal("fetch", fetchMock);
    let res = await GET(req("GET", "sources"), ctx("sources"));
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "bad name" });
    fetchMock = upstream({ error: "boom" }, 503);
    vi.stubGlobal("fetch", fetchMock);
    res = await GET(req("GET", "sources"), ctx("sources"));
    expect(res.status).toBe(503);
    expect(await res.json()).toEqual({ error: "boom" });
    expect([...res.headers.keys()].filter((k) => k !== "content-type")).toEqual([]);
  });

  it("returns 502 when the control API is unreachable, without the token", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("ECONNREFUSED"); }));
    const res = await GET(req("GET", "me"), ctx("me"));
    expect(res.status).toBe(502);
    expect(JSON.stringify(await res.json())).not.toContain("SECRET-TOKEN");
  });

  it("returns 502 when CONTROL_API_URL is unset", async () => {
    delete process.env.CONTROL_API_URL;
    const res = await GET(req("GET", "me"), ctx("me"));
    expect(res.status).toBe(502);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("controlFetch", () => {
  it("returns ok data and never exposes the token", async () => {
    fetchMock = upstream({ is_admin: true });
    vi.stubGlobal("fetch", fetchMock);
    const r = await controlFetch("api/v1/me");
    expect(r).toEqual({ ok: true, data: { is_admin: true }, status: 200 });
    expect(JSON.stringify(r)).not.toContain("SECRET-TOKEN");
  });

  it("maps upstream errors", async () => {
    vi.stubGlobal("fetch", upstream({ error: "nope" }, 403));
    expect(await controlFetch("api/v1/sources")).toEqual({ ok: false, status: 403, error: "nope" });
  });
});
