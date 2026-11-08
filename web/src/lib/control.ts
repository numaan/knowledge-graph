import "server-only";

import { accessToken } from "@/auth";
import type { Result } from "./types";

/** Like Result, but a success also carries the upstream status (200, 201, ...). */
export type ControlResult<T> = (Extract<Result<T>, { ok: true }> & { status: number }) | Extract<Result<T>, { ok: false }>;

// Server-side client of the control API (admin: sources, runs, audit). Browser code goes through
// /api/admin (route handler). The access token is added here and never returned to callers.

export const controlBase = () => (process.env.CONTROL_API_URL ?? "").trim().replace(/\/+$/, "");

/** `path` is the control API path below the base, for example `api/v1/sources`. */
export async function controlFetch<T>(path: string, init: RequestInit & { timeoutMs?: number } = {}): Promise<ControlResult<T>> {
  const base = controlBase();
  if (!base) return { ok: false, status: 502, error: "control API not configured" };
  let url: URL;
  try {
    url = new URL(`${base}/${path.replace(/^\/+/, "")}`);
  } catch {
    return { ok: false, status: 502, error: "control API not configured" };
  }
  const headers = new Headers();
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  const token = await accessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let res: Response;
  try {
    res = await fetch(url, { method: init.method, body: init.body, headers, cache: "no-store", redirect: "manual", signal: AbortSignal.timeout(init.timeoutMs ?? 30_000) });
  } catch {
    return { ok: false, status: 502, error: "control API not reachable" };
  }
  const body = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok) return { ok: false, status: res.status, error: body?.error ?? res.statusText };
  return { ok: true, data: body as T, status: res.status };
}
