// Pure helpers for the /api/admin proxy to the control API: path allow-list, CSRF check, body limit.
//
// CSRF mechanism (one mechanism, two conditions, both required for POST, PATCH and DELETE):
//   1. Same origin: the `Origin` header equals the request origin, or `Sec-Fetch-Site` is `same-origin`.
//   2. Custom header `X-KG-Admin: 1`. A cross-site form cannot set it, and a cross-site fetch needs a
//      CORS preflight that this app never grants.
// The admin client code (added in T14) must send the header on every mutating call.

const ID = /^[A-Za-z0-9_-]+$/;

export const ADMIN_HEADER = "x-kg-admin";
export const ADMIN_HEADER_VALUE = "1";
export const MAX_BODY_BYTES = 1024 * 1024;

export type ControlMatch = { path: string; methods: string[] };

function decodeOnce(s: string): string | null {
  try {
    return decodeURIComponent(s);
  } catch {
    return null;
  }
}

/** A segment is safe when it is non-empty and holds no separator, dot, `%`, or control character, before or after one decode. */
function plainSegment(s: string): boolean {
  if (typeof s !== "string" || !s) return false;
  const d = decodeOnce(s);
  if (d === null) return false;
  for (const v of [s, d]) {
    if (v === "." || v === ".." || /[/\\%\0]/.test(v)) return false;
  }
  return true;
}

/**
 * Map proxy path segments to the control API path (`api/v1/...`) and the allowed methods, or null.
 * Allowed: me, audit, sources, sources/{id}, sources/{id}/test, sources/{id}/run, runs, runs/{id},
 * runs/{id}/cancel. Ids must match ^[A-Za-z0-9_-]+$.
 */
export function controlPath(path: string[]): ControlMatch | null {
  if (!Array.isArray(path) || path.length === 0 || path.length > 3) return null;
  if (!path.every(plainSegment)) return null;
  const [head, id, action] = path;
  const out = (rest: string, methods: string[]): ControlMatch => ({ path: `api/v1/${rest}`, methods });
  if (path.length === 1) {
    if (head === "me" || head === "audit") return out(head, ["GET"]);
    if (head === "sources") return out(head, ["GET", "POST"]);
    if (head === "runs") return out(head, ["GET"]);
    return null;
  }
  if (!ID.test(id)) return null;
  if (path.length === 2) {
    if (head === "sources") return out(`sources/${id}`, ["GET", "PATCH", "DELETE"]);
    if (head === "runs") return out(`runs/${id}`, ["GET"]);
    return null;
  }
  if (head === "sources" && (action === "test" || action === "run")) return out(`sources/${id}/${action}`, ["POST"]);
  if (head === "runs" && action === "cancel") return out(`runs/${id}/cancel`, ["POST"]);
  return null;
}

/** True when `method` is allowed for the path (case-insensitive). */
export function methodAllowed(match: ControlMatch, method: string): boolean {
  return match.methods.includes(method.toUpperCase());
}

export function isMutating(method: string): boolean {
  return ["POST", "PATCH", "DELETE"].includes(method.toUpperCase());
}

/** CSRF check. Non-mutating methods pass; mutating ones need same origin AND the custom header. */
/**
 * The origin the browser used for this request. `req.url` of a Next server holds the bind address
 * (for example http://localhost:3000), not the public host, so the Host header (or the first
 * X-Forwarded-Host of the ingress) is used. The scheme comes from X-Forwarded-Proto; without it, the
 * scheme of the Origin header is used (host and port are still compared). A cross-site page cannot
 * set these headers: it needs a CORS preflight that this app never grants.
 */
export function requestOrigin(headers: Headers, url: string): string {
  const u = new URL(url);
  const first = (v: string | null) => v?.split(",")[0].trim() || null;
  let originProto: string | null = null;
  try {
    const o = headers.get("origin");
    originProto = o ? new URL(o).protocol.replace(":", "") : null;
  } catch {
    originProto = null;
  }
  const host = first(headers.get("x-forwarded-host")) ?? first(headers.get("host")) ?? u.host;
  const proto = first(headers.get("x-forwarded-proto")) ?? originProto ?? u.protocol.replace(":", "");
  return `${proto}://${host}`;
}

export function csrfOk(method: string, headers: Headers, requestOrigin: string): boolean {
  if (!isMutating(method)) return true;
  if (headers.get(ADMIN_HEADER) !== ADMIN_HEADER_VALUE) return false;
  const origin = headers.get("origin");
  if (origin !== null) return origin === requestOrigin;
  return headers.get("sec-fetch-site") === "same-origin";
}

/** True when the declared Content-Length is over the limit. */
export function declaredTooLarge(headers: Headers, max = MAX_BODY_BYTES): boolean {
  const n = Number(headers.get("content-length"));
  return Number.isFinite(n) && n > max;
}

/** Read a body into bytes. Returns null when it is over `max` bytes (stops reading early). */
export async function readLimited(body: ReadableStream<Uint8Array> | null, max = MAX_BODY_BYTES): Promise<Uint8Array | null> {
  if (!body) return new Uint8Array(0);
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > max) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.byteLength;
  }
  return out;
}
