import "server-only";

import { accessToken } from "@/auth";
import { upstreamUrl } from "./proxy-path";
import type { NodeResponse, RequirementsResponse, Result, ToolInfo } from "./types";

// Server-side client of the kg-mcp REST layer. Browser code goes through /api/kg (route handler).

export const apiBase = () => (process.env.KG_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export async function kgFetch<T>(path: string, init: RequestInit = {}): Promise<Result<T>> {
  // Resolve the URL first and refuse anything that normalises to a path outside <base>/api/.
  const url = upstreamUrl(apiBase(), path);
  if (!url) return { ok: false, status: 404, error: "not found" };
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  const token = await accessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let res: Response;
  try {
    res = await fetch(url, { ...init, headers, cache: "no-store", signal: AbortSignal.timeout(130_000) });
  } catch (e) {
    return { ok: false, status: 502, error: `knowledge graph API not reachable (${(e as Error).message})` };
  }
  const body = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok) return { ok: false, status: res.status, error: body?.error ?? res.statusText };
  return { ok: true, data: body as T };
}

export const callTool = <T>(name: string, args: Record<string, unknown>) =>
  kgFetch<T>(`tools/${encodeURIComponent(name)}`, { method: "POST", body: JSON.stringify(args) });

export const listTools = () => kgFetch<{ tools: ToolInfo[] }>("tools");

export const getNode = (id: string) => kgFetch<NodeResponse>(`node/${encodeURIComponent(id)}`);

export const getRequirements = (repo = "") =>
  kgFetch<RequirementsResponse>(`requirements${repo ? `?repo=${encodeURIComponent(repo)}` : ""}`);
