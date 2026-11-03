import type { GraphNode, Neighbor, Props } from "./types";

// Label-driven entity layout: one generic page for every node kind.

const KINDS = [
  "Symbol", "File", "Ticket", "Section", "Document", "Requirement", "PullRequest", "Commit", "Release",
  "Person", "Team", "Service", "API", "System", "Contract", "DomainConcept", "BusinessRule", "Capability",
  "Repository", "Module", "Chunk",
];
const LAYER_LABELS = new Set(["Code", "Source", "Domain"]);

export function entityKind(labels: string[]): string {
  return KINDS.find((k) => labels.includes(k)) ?? labels.find((l) => !LAYER_LABELS.has(l)) ?? labels[0] ?? "Node";
}

export const entityHref = (id: string) => `/entity?id=${encodeURIComponent(id)}`;

export function entityTitle(node: GraphNode): string {
  const p = node.properties;
  const t = p.qualname ?? p.title ?? p.name ?? p.key ?? p.ref ?? p.statement ?? p.path ?? p.sha ?? p.id;
  return String(t ?? "untitled");
}

/** Link to the original system: Jira, Confluence, GitHub, or a document URL. */
export function sourceLink(p: Props): string | undefined {
  for (const k of ["url", "html_url", "web_url", "source_url"]) {
    const v = p[k];
    if (typeof v === "string" && /^https?:\/\//.test(v)) return v;
  }
  return undefined;
}

export interface Badges {
  stale: boolean;
  conflict: boolean;
}

export function badges(p: Props, neighbors: Neighbor[] = []): Badges {
  const stale = p.stale === true || p.freshness === "stale" || p.status === "stale";
  const conflicts = Array.isArray(p.conflicts) && p.conflicts.length > 0;
  const conflictEdge = neighbors.some((n) => n.via?.some((t) => t === "CONTRADICTED_BY" || t === "CONFLICTS_WITH"));
  return { stale, conflict: p.conflict === true || conflicts || conflictEdge || p.verdict === "contradicted" };
}

/** Neighbours grouped by the first edge type on the path, largest group first. */
export function groupNeighbors(rows: Neighbor[]): [string, Neighbor[]][] {
  const groups = new Map<string, Neighbor[]>();
  for (const r of rows) {
    const key = r.via?.[0] ?? "RELATED";
    groups.set(key, [...(groups.get(key) ?? []), r]);
  }
  return [...groups.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
}

// Shown elsewhere on the page or not useful to humans.
const HIDDEN = new Set(["id", "content_hash", "embedding", "embedding_hash", "text", "code", "acl", "email_hashes"]);
export const LONG_TEXT = ["text", "body", "description", "statement", "summary", "definition", "reasoning"];

export function displayProps(p: Props): [string, string][] {
  return Object.entries(p)
    .filter(([k, v]) => !HIDDEN.has(k) && v != null && v !== "" && !(Array.isArray(v) && v.length === 0))
    .filter(([k, v]) => !(LONG_TEXT.includes(k) && typeof v === "string" && v.length > 160))
    .map(([k, v]) => [k, Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : String(v)] as [string, string])
    .sort((a, b) => a[0].localeCompare(b[0]));
}

const EXT: Record<string, string> = {
  ts: "typescript", tsx: "tsx", js: "javascript", jsx: "jsx", mjs: "javascript", py: "python", go: "go",
  java: "java", kt: "kotlin", kts: "kotlin", cs: "csharp", sql: "sql", json: "json", yaml: "yaml", yml: "yaml",
  md: "markdown", sh: "bash", html: "html", css: "css", vue: "vue",
};

export function languageOf(path: unknown): string {
  const ext = typeof path === "string" ? path.split(".").pop()?.toLowerCase() ?? "" : "";
  return EXT[ext] ?? "text";
}
