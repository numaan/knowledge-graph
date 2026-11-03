import type { Answer, Citation } from "./types";

// The ask tool's result, normalised. Accepted shapes: {data: {answer, citations}} or {answer, citations};
// citations may be called sources, and each citation may name its title as title or name.

const str = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : String(v));

export function normaliseAnswer(raw: unknown): Answer {
  const r = (raw ?? {}) as Record<string, unknown>;
  const d = (r.data && typeof r.data === "object" ? r.data : r) as Record<string, unknown>;
  const list = (Array.isArray(d.citations) ? d.citations : Array.isArray(d.sources) ? d.sources : []) as Record<string, unknown>[];
  const citations: Citation[] = list.map((c, i) => ({
    n: typeof c.n === "number" ? c.n : i + 1,
    id: c.id ? str(c.id) : undefined,
    title: str(c.title ?? c.name ?? c.id ?? `Source ${i + 1}`),
    url: c.url ? str(c.url) : undefined,
    kind: c.kind ? str(c.kind) : undefined,
    snippet: c.snippet ? str(c.snippet) : undefined,
  }));
  return {
    answer: str(d.answer ?? d.text ?? ""),
    citations,
    confidence: typeof d.confidence === "number" ? d.confidence : undefined,
    note: d.note ? str(d.note) : undefined,
  };
}

export type Segment = { text: string } | { cite: number };

/** Split answer text at citation markers such as [1] or [2, 3]. Unknown numbers stay plain text. */
export function splitCitations(text: string, known: Set<number>): Segment[] {
  const out: Segment[] = [];
  const re = /\[(\d+(?:\s*,\s*\d+)*)\]/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    const nums = m[1].split(",").map((s) => Number(s.trim()));
    if (!nums.every((n) => known.has(n))) continue;
    if (m.index > last) out.push({ text: text.slice(last, m.index) });
    for (const n of nums) out.push({ cite: n });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last) });
  return out;
}
