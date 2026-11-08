import "server-only";

import { controlBase, controlFetch } from "./control";
import type { MeResult } from "./control-types";

export type MeState = { state: "admin"; user: string } | { state: "forbidden" } | { state: "unavailable" };

/** Asks the control API who the caller is. Never throws; no call when CONTROL_API_URL is unset. */
export async function getMe(timeoutMs = 2000): Promise<MeState> {
  if (!controlBase()) return { state: "unavailable" };
  try {
    const r = await controlFetch<MeResult>("api/v1/me", { timeoutMs });
    if (r.ok) return r.data?.is_admin === true ? { state: "admin", user: r.data.user } : { state: "forbidden" };
    return r.status === 403 ? { state: "forbidden" } : { state: "unavailable" };
  } catch {
    return { state: "unavailable" };
  }
}
