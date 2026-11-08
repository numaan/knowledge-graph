import { describe, expect, it } from "vitest";
import { controlPath, csrfOk, declaredTooLarge, methodAllowed, readLimited, requestOrigin, MAX_BODY_BYTES } from "@/lib/admin-proxy";

const p = (s: string) => controlPath(s.split("/"));

describe("controlPath", () => {
  it("maps allow-listed paths", () => {
    expect(p("me")?.path).toBe("api/v1/me");
    expect(p("audit")?.path).toBe("api/v1/audit");
    expect(p("sources")?.path).toBe("api/v1/sources");
    expect(p("sources/a-1_b")?.path).toBe("api/v1/sources/a-1_b");
    expect(p("sources/x/test")?.path).toBe("api/v1/sources/x/test");
    expect(p("sources/x/run")?.path).toBe("api/v1/sources/x/run");
    expect(p("runs")?.path).toBe("api/v1/runs");
    expect(p("runs/r1")?.path).toBe("api/v1/runs/r1");
    expect(p("runs/r1/cancel")?.path).toBe("api/v1/runs/r1/cancel");
  });
  it("rejects unknown and malformed paths", () => {
    for (const s of ["", "healthz", "sources/x/delete", "runs/x/run", "me/x", "audit/x", "sources/x/test/y", "runs/x/test"]) {
      expect(p(s)).toBeNull();
    }
    expect(controlPath([])).toBeNull();
    expect(controlPath(undefined as unknown as string[])).toBeNull();
  });
  it("rejects traversal and bad ids", () => {
    const bad = ["..", ".", "%2E%2E", "%2e%2e", "a%2Fb", "a%5Cb", "a\\b", "a/b", "%252E%252E", "%2525", "a%00b", "a\0b", "a b", "a.b", "%E0%A4%A", ""];
    for (const b of bad) {
      expect(controlPath(["sources", b]), b).toBeNull();
      expect(controlPath(["runs", "x", b]), b).toBeNull();
    }
    expect(controlPath(["sources", "", "run"])).toBeNull();
    expect(controlPath(["sources", "x", ""])).toBeNull();
    expect(controlPath(["..", "me"])).toBeNull();
  });
  it("checks methods per path", () => {
    expect(methodAllowed(p("sources")!, "post")).toBe(true);
    expect(methodAllowed(p("sources")!, "DELETE")).toBe(false);
    expect(methodAllowed(p("sources/x")!, "PATCH")).toBe(true);
    expect(methodAllowed(p("sources/x")!, "DELETE")).toBe(true);
    expect(methodAllowed(p("sources/x")!, "POST")).toBe(false);
    expect(methodAllowed(p("sources/x/run")!, "GET")).toBe(false);
    expect(methodAllowed(p("sources/x/run")!, "POST")).toBe(true);
    expect(methodAllowed(p("runs")!, "POST")).toBe(false);
    expect(methodAllowed(p("runs/x/cancel")!, "POST")).toBe(true);
    expect(methodAllowed(p("me")!, "PUT")).toBe(false);
  });
});

describe("csrfOk", () => {
  const o = "https://kg.example.com";
  const h = (x: Record<string, string>) => new Headers(x);
  it("lets GET pass without headers", () => {
    expect(csrfOk("GET", h({}), o)).toBe(true);
  });
  it("needs the custom header on mutating methods", () => {
    for (const m of ["POST", "PATCH", "DELETE"]) {
      expect(csrfOk(m, h({ origin: o }), o)).toBe(false);
      expect(csrfOk(m, h({ origin: o, "x-kg-admin": "0" }), o)).toBe(false);
      expect(csrfOk(m, h({ origin: o, "x-kg-admin": "1" }), o)).toBe(true);
    }
  });
  it("needs same origin", () => {
    expect(csrfOk("POST", h({ origin: "https://evil.example", "x-kg-admin": "1" }), o)).toBe(false);
    expect(csrfOk("POST", h({ origin: "https://evil.example", "sec-fetch-site": "same-origin", "x-kg-admin": "1" }), o)).toBe(false);
    expect(csrfOk("POST", h({ "sec-fetch-site": "same-origin", "x-kg-admin": "1" }), o)).toBe(true);
    expect(csrfOk("POST", h({ "sec-fetch-site": "cross-site", "x-kg-admin": "1" }), o)).toBe(false);
    expect(csrfOk("POST", h({ "x-kg-admin": "1" }), o)).toBe(false);
  });
});

describe("body limit", () => {
  const stream = (n: number) =>
    new Response(new Uint8Array(n)).body as ReadableStream<Uint8Array>;
  it("checks declared length", () => {
    expect(declaredTooLarge(new Headers({ "content-length": String(MAX_BODY_BYTES + 1) }))).toBe(true);
    expect(declaredTooLarge(new Headers({ "content-length": String(MAX_BODY_BYTES) }))).toBe(false);
    expect(declaredTooLarge(new Headers())).toBe(false);
  });
  it("reads up to the limit and rejects more", async () => {
    expect((await readLimited(stream(MAX_BODY_BYTES)))?.byteLength).toBe(MAX_BODY_BYTES);
    expect(await readLimited(stream(MAX_BODY_BYTES + 1))).toBeNull();
    expect((await readLimited(null))?.byteLength).toBe(0);
  });
});

describe("requestOrigin", () => {
  const h = (o: Record<string, string>) => new Headers(o);
  it("uses the Host header, not the bind address of req.url", () => {
    expect(requestOrigin(h({ host: "127.0.0.1:3100", origin: "http://127.0.0.1:3100" }), "http://localhost:3100/api/admin/x")).toBe("http://127.0.0.1:3100");
  });
  it("prefers the first forwarded host and proto of the ingress", () => {
    const headers = h({ host: "web:3000", "x-forwarded-host": "kg.example.com, web", "x-forwarded-proto": "https, http", origin: "https://kg.example.com" });
    expect(requestOrigin(headers, "http://localhost:3000/x")).toBe("https://kg.example.com");
  });
  it("falls back to req.url without headers", () => {
    expect(requestOrigin(h({}), "http://localhost:3000/x")).toBe("http://localhost:3000");
  });
  it("does not accept a foreign origin", () => {
    const o = requestOrigin(h({ host: "kg.example.com", origin: "https://evil.example" }), "http://localhost:3000/x");
    expect(csrfOk("POST", h({ "x-kg-admin": "1", origin: "https://evil.example" }), o)).toBe(false);
  });
});
