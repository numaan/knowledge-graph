import type { Result } from "./types";

// Browser-side client of the admin proxy (/api/admin/...). Mutating calls always send the
// X-KG-Admin header that the proxy requires as CSRF protection.

export type AdminResult<T> = (Extract<Result<T>, { ok: true }> & { status: number }) | Extract<Result<T>, { ok: false }>;

async function call<T>(method: string, path: string, body?: unknown): Promise<AdminResult<T>> {
  const headers = new Headers({ Accept: "application/json" });
  if (method !== "GET") headers.set("X-KG-Admin", "1");
  if (body !== undefined) headers.set("Content-Type", "application/json");
  let res: Response;
  try {
    res = await fetch(`/api/admin/${path.replace(/^\/+/, "")}`, {
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    return { ok: false, status: 502, error: "control API not reachable" };
  }
  const json = await res.json().catch(() => null);
  if (!res.ok) return { ok: false, status: res.status, error: json?.error ?? res.statusText ?? "request failed" };
  return { ok: true, status: res.status, data: json as T };
}

export const adminApi = {
  get: <T = unknown>(path: string) => call<T>("GET", path),
  post: <T = unknown>(path: string, body?: unknown) => call<T>("POST", path, body ?? {}),
  patch: <T = unknown>(path: string, body: unknown) => call<T>("PATCH", path, body),
  del: <T = unknown>(path: string) => call<T>("DELETE", path),
};
