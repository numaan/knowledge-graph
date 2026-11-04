// Path validation for the /api/kg proxy. Next.js hands the route the catch-all segments already
// percent-decoded, so `%2E%2E` arrives as `..` and `%2F` as a `/` inside a segment. Joining such
// segments and letting fetch() normalise the URL would let a request climb out of the allowlisted
// REST routes (e.g. /api/kg/node/%2E%2E/%2E%2E/mcp reaching kg-mcp's /mcp with the user's token).

const TOOL = /^[a-z0-9_]+$/;

function decodeOnce(s: string): string | null {
  try {
    return decodeURIComponent(s);
  } catch {
    return null;
  }
}

/** True for `.`/`..`, also when still percent-encoded once more (e.g. `%2E%2E`). */
function isDotSegment(s: string): boolean {
  const d = decodeOnce(s);
  return s === "." || s === ".." || d === "." || d === "..";
}

/** A plain path segment: non-empty, no separators, not a dot segment (before or after decoding). */
function safeSegment(s: string): boolean {
  if (!s || isDotSegment(s)) return false;
  const d = decodeOnce(s) ?? "";
  return !/[/\\]/.test(s) && !/[/\\]/.test(d);
}

/**
 * Map the proxy's path segments to the kg-mcp path below `/api/`, or null when the path is not an
 * allowed route. Every segment of the result is percent-encoded, so it cannot contain `/`, `..` or
 * anything else a URL parser would resolve.
 *
 * Allowed: tools, tools/<name>, requirements, feedback, node/<id>. A node id is opaque and may
 * contain `/` (file paths), so it is sent as one encoded segment; it must not contain `\` or any
 * `.`/`..` path component.
 */
export function upstreamPath(path: string[]): string | null {
  if (!Array.isArray(path) || path.length === 0) return null;
  const [head, ...rest] = path;
  if (head === "node") {
    const id = rest.join("/");
    if (!id || id.includes("\\")) return null;
    const parts = id.split("/");
    if (parts.some((p) => !p || isDotSegment(p))) return null;
    const decoded = decodeOnce(id) ?? id;
    if (decoded.includes("\\") || decoded.split("/").some((p) => p === "." || p === "..")) return null;
    return `node/${encodeURIComponent(id)}`;
  }
  if (!path.every(safeSegment)) return null;
  if (path.length === 1 && (head === "tools" || head === "requirements" || head === "feedback")) return head;
  if (path.length === 2 && head === "tools" && TOOL.test(rest[0])) return `tools/${rest[0]}`;
  return null;
}

/**
 * The full upstream URL for `path` (relative to `/api/`, may carry a query string), or null when the
 * resolved URL would leave `<base>/api/`.
 */
export function upstreamUrl(base: string, path: string): URL | null {
  const root = new URL(`${base.replace(/\/$/, "")}/api/`);
  let url: URL;
  try {
    url = new URL(`${root.href}${path}`);
  } catch {
    return null;
  }
  if (url.origin !== root.origin || !url.pathname.startsWith(root.pathname) || url.pathname === root.pathname) return null;
  return url;
}
