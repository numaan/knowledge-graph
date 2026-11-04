import { normaliseAnswer } from "@/lib/answer";
import { upstreamPath, upstreamUrl } from "@/lib/proxy-path";
import { needsRefresh, REFRESH_ERROR, refreshAccessToken, rotateToken, tokenEndpoint } from "@/lib/token-refresh";

// Next.js passes catch-all segments percent-decoded (split on raw "/", then decodeURIComponent).
const nextSegments = (raw: string) => raw.split("/").map(decodeURIComponent);

describe("kg proxy path validation", () => {
  const BASE = "http://kg-mcp:8000";
  const resolve = (raw: string) => {
    const p = upstreamPath(nextSegments(raw));
    return p === null ? null : upstreamUrl(BASE, p)?.pathname ?? null;
  };

  it("forwards the allowlisted routes", () => {
    expect(upstreamPath(["tools"])).toBe("tools");
    expect(upstreamPath(["tools", "ask"])).toBe("tools/ask");
    expect(upstreamPath(["requirements"])).toBe("requirements");
    expect(upstreamPath(["feedback"])).toBe("feedback");
    expect(upstreamPath(["node", "jira:HR-1"])).toBe("node/jira%3AHR-1");
  });

  it("keeps node ids with slashes as one encoded segment", () => {
    expect(resolve("node/precept%3Aapp%2Fmodels.py%23Invite")).toBe("/api/node/precept%3Aapp%2Fmodels.py%23Invite");
    expect(upstreamPath(["node", "src", "a.ts"])).toBe("node/src%2Fa.ts");
  });

  it.each([
    "node/%2E%2E/%2E%2E/mcp",
    "node/../../mcp",
    "node/./x",
    "node/a%2F..%2F..%2Fmcp",
    "node/%252E%252E/mcp",
    "node/a%5C..%5Cmcp",
    "node",
    "node/",
    "tools/%2E%2E",
    "tools/..",
    "tools/a%2Fb",
    "tools/a%5Cb",
    "tools/Ask",
    "tools/ask/x",
    "mcp",
    "requirements/..",
    "feedback/%2E",
    "",
  ])("rejects %s", (raw) => {
    expect(upstreamPath(nextSegments(raw))).toBeNull();
  });

  it("rejects empty and non-array input", () => {
    expect(upstreamPath([])).toBeNull();
    expect(upstreamPath(undefined as unknown as string[])).toBeNull();
  });

  it("resolves only below <base>/api/", () => {
    expect(upstreamUrl(BASE, "tools/ask?x=1")?.href).toBe("http://kg-mcp:8000/api/tools/ask?x=1");
    expect(upstreamUrl(`${BASE}/prefix/`, "tools")?.pathname).toBe("/prefix/api/tools");
    expect(upstreamUrl(BASE, "node/../../mcp")).toBeNull();
    expect(upstreamUrl(BASE, "node/%2E%2E/%2E%2E/mcp")).toBeNull();
    expect(upstreamUrl(BASE, "..")).toBeNull();
    expect(upstreamUrl(BASE, "")).toBeNull();
    expect(upstreamUrl(BASE, "//evil.example/api/x")?.origin).toBe("http://kg-mcp:8000");
    expect(upstreamUrl(`${BASE}/prefix`, "../mcp")).toBeNull();
  });
});

describe("normaliseAnswer with the Go ask tool's citations", () => {
  it("maps label to kind and excerpt to snippet", () => {
    const a = normaliseAnswer({
      answer: "x [1]",
      citations: [{ n: 1, id: "jira:HR-1", label: "Ticket", title: "HR-1", path: "", url: "https://j/HR-1", excerpt: "Invite flow" }],
      confidence: 0.8,
    });
    expect(a.citations[0]).toEqual({ n: 1, id: "jira:HR-1", title: "HR-1", url: "https://j/HR-1", kind: "Ticket", snippet: "Invite flow" });
  });

  it("falls back to kind and snippet, preferring the Go names", () => {
    expect(normaliseAnswer({ citations: [{ title: "A", kind: "Doc", snippet: "s" }] }).citations[0]).toMatchObject({ kind: "Doc", snippet: "s" });
    expect(normaliseAnswer({ citations: [{ title: "A", label: "L", kind: "K", excerpt: "e", snippet: "s" }] }).citations[0]).toMatchObject({ kind: "L", snippet: "e" });
    expect(normaliseAnswer({ citations: [{ title: "A", label: "", kind: "K", excerpt: "", snippet: "s" }] }).citations[0]).toMatchObject({ kind: "K", snippet: "s" });
  });
});

describe("access token refresh", () => {
  const cfg = { issuer: "https://kc/realms/kg/", clientId: "web", clientSecret: "sec" };
  const NOW = 1_000_000;
  const now = () => NOW;
  const okFetch = (body: Record<string, unknown>, status = 200) =>
    vi.fn(async (_url: string | URL | Request, _init?: RequestInit) => new Response(JSON.stringify(body), { status }));

  it("stores access token, refresh token and expiry at sign-in", async () => {
    const t = await rotateToken({ sub: "u" }, { access_token: "a1", refresh_token: "r1", expires_at: NOW + 900 }, cfg, okFetch({}), now);
    expect(t).toMatchObject({ sub: "u", access_token: "a1", refresh_token: "r1", expires_at: NOW + 900 });
  });

  it("leaves a fresh token alone", async () => {
    const f = okFetch({});
    const tok = { access_token: "a1", refresh_token: "r1", expires_at: NOW + 600 };
    expect(await rotateToken(tok, undefined, cfg, f, now)).toBe(tok);
    expect(f).not.toHaveBeenCalled();
  });

  it("knows when a refresh is due", () => {
    expect(needsRefresh({ access_token: "a", expires_at: NOW + 30 }, NOW)).toBe(true);
    expect(needsRefresh({ access_token: "a", expires_at: NOW - 1 }, NOW)).toBe(true);
    expect(needsRefresh({ access_token: "a", expires_at: NOW + 600 }, NOW)).toBe(false);
  });

  it("refreshes near expiry with the refresh_token grant", async () => {
    const f = okFetch({ access_token: "a2", refresh_token: "r2", expires_in: 900 });
    const t = await rotateToken({ access_token: "a1", refresh_token: "r1", expires_at: NOW + 10 }, undefined, cfg, f, now);
    expect(t).toMatchObject({ access_token: "a2", refresh_token: "r2", expires_at: NOW + 900 });
    expect(t.error).toBeUndefined();
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("https://kc/realms/kg/protocol/openid-connect/token");
    expect(tokenEndpoint(cfg.issuer)).toBe(url);
    const sent = new URLSearchParams(String(init?.body));
    expect(Object.fromEntries(sent)).toEqual({ grant_type: "refresh_token", refresh_token: "r1", client_id: "web", client_secret: "sec" });
  });

  it("keeps the old refresh token when Keycloak does not rotate it", async () => {
    const t = await refreshAccessToken({ access_token: "a1", refresh_token: "r9", expires_at: NOW }, cfg, okFetch({ access_token: "a2", expires_in: 300 }), now);
    expect(t).toMatchObject({ access_token: "a2", refresh_token: "r9", expires_at: NOW + 300 });
  });

  it("sets an error and drops the access token when the refresh fails", async () => {
    const t = await rotateToken({ access_token: "a1", refresh_token: "r3", expires_at: NOW }, undefined, cfg, okFetch({ error: "invalid_grant" }, 400), now);
    expect(t.error).toBe(REFRESH_ERROR);
    expect(t.access_token).toBeUndefined();
    const net = vi.fn(async () => {
      throw new Error("down");
    });
    expect((await refreshAccessToken({ access_token: "a", refresh_token: "r4" }, cfg, net, now)).error).toBe(REFRESH_ERROR);
    expect((await refreshAccessToken({ access_token: "a" }, cfg, okFetch({}), now)).error).toBe(REFRESH_ERROR);
  });

  it("does not retry once the token carries an error", async () => {
    const f = okFetch({ access_token: "a2" });
    const tok = { refresh_token: "r5", expires_at: NOW - 100, error: REFRESH_ERROR };
    expect(await rotateToken(tok, undefined, cfg, f, now)).toBe(tok);
    expect(f).not.toHaveBeenCalled();
  });

  it("shares one refresh call between concurrent requests", async () => {
    const f = okFetch({ access_token: "a2", refresh_token: "r7", expires_in: 900 });
    const tok = { access_token: "a1", refresh_token: "r6", expires_at: NOW };
    const [x, y] = await Promise.all([refreshAccessToken(tok, cfg, f, now), refreshAccessToken(tok, cfg, f, now)]);
    expect(f).toHaveBeenCalledTimes(1);
    expect(x.access_token).toBe("a2");
    expect(y.access_token).toBe("a2");
  });
});
