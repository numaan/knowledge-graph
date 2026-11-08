import type { RunStatus } from "@/lib/control-types";

/** Fixed UTC format, the same on server and browser (no hydration drift). */
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export function isActive(status: RunStatus): boolean {
  return status === "queued" || status === "running";
}

export function fmtDuration(start: string | null | undefined, end: string | null | undefined, now = Date.now()): string {
  if (!start) return "-";
  const s = new Date(start).getTime();
  if (Number.isNaN(s)) return "-";
  const e = end ? new Date(end).getTime() : now;
  if (Number.isNaN(e)) return "-";
  const sec = Math.max(0, Math.round((e - s) / 1000));
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ${sec % 60}s`;
  return `${Math.floor(min / 60)}h ${min % 60}m`;
}

/** Compact key=value text of an audit detail. Plain text only; React escapes it. */
export function kvText(detail: Record<string, unknown> | null | undefined): string {
  if (!detail) return "";
  return Object.entries(detail)
    .map(([k, v]) => {
      if (typeof v === "string" && /^[^\s"=]+$/.test(v)) return `${k}=${v}`;
      return `${k}=${JSON.stringify(v)}`;
    })
    .join(" ");
}

/** Poll delay in ms: 3 s, or 10 s after 5 consecutive errors. */
export function pollDelay(consecutiveErrors: number): number {
  return consecutiveErrors >= 5 ? 10000 : 3000;
}

export const LIMITS = [50, 100, 200] as const;

export function parseLimit(raw: string | string[] | undefined): number {
  const v = Number(Array.isArray(raw) ? raw[0] : raw);
  return (LIMITS as readonly number[]).includes(v) ? v : 50;
}

export function statusClass(status: RunStatus): string {
  if (status === "succeeded") return "ok";
  if (status === "failed") return "bad";
  if (status === "running" || status === "queued") return "warn";
  return "";
}
