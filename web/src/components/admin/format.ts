import type { RunStatus } from "@/lib/control-types";

/** Fixed UTC format, the same on server and browser (no hydration drift). */
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export function statusClass(status: RunStatus): string {
  if (status === "succeeded") return "ok";
  if (status === "failed") return "bad";
  if (status === "running" || status === "queued") return "warn";
  return "";
}
