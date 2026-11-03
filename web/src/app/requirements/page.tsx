import type { Metadata } from "next";
import Link from "next/link";
import { ToolError } from "@/components/ToolError";
import { entityHref } from "@/lib/entity";
import { getRequirements } from "@/lib/kg";
import type { RequirementRow } from "@/lib/types";

export const metadata: Metadata = { title: "Requirement coverage" };

const VERDICTS = [
  ["supported", "ok", "var(--ok)"],
  ["contradicted", "bad", "var(--bad)"],
  ["unresolved", "warn", "var(--warn)"],
] as const;

type SP = Promise<{ repo?: string; verdict?: string }>;

export default async function RequirementsPage({ searchParams }: { searchParams: SP }) {
  const { repo = "", verdict = "" } = await searchParams;
  const res = await getRequirements(repo);
  if (!res.ok) {
    return (<><h1>Requirement coverage</h1><ToolError r={res} what="Requirement coverage" /></>);
  }
  const { counts, total, requirements } = res.data;
  const shown = verdict ? requirements.filter((r) => (r.verdict ?? "unresolved") === verdict) : requirements;
  const bySource = new Map<string, RequirementRow[]>();
  for (const r of shown) bySource.set(r.source ?? "(no source)", [...(bySource.get(r.source ?? "(no source)") ?? []), r]);
  const q = (v: string) => `/requirements?${new URLSearchParams({ ...(repo && { repo }), ...(v && { verdict: v }) })}`;

  return (
    <>
      <h1>Requirement coverage</h1>
      <p className="muted">Requirements checked by <code>kg-verify</code>, with the code that implements or contradicts them.</p>
      {total === 0 ? <p className="notice">No checked requirements yet. Run <code>kg-verify</code> on a document or ticket.</p> : (
        <>
          <div className="bar" role="img" aria-label={VERDICTS.map(([v]) => `${counts[v] ?? 0} ${v}`).join(", ")}>
            {VERDICTS.map(([v, , color]) => <span key={v} style={{ width: `${(100 * (counts[v] ?? 0)) / total}%`, background: color }} />)}
          </div>
          <nav className="tabs" aria-label="Filter by verdict">
            <Link href={q("")} aria-current={verdict === "" ? "true" : undefined}>All ({total})</Link>
            {VERDICTS.map(([v]) => (
              <Link key={v} href={q(v)} aria-current={verdict === v ? "true" : undefined}>{v} ({counts[v] ?? 0})</Link>
            ))}
          </nav>
        </>
      )}
      {[...bySource.entries()].map(([source, rows]) => (
        <section key={source} aria-label={source}>
          <h2 style={{ overflowWrap: "anywhere" }}>{source}</h2>
          <ul className="list">
            {rows.map((r) => <Requirement key={r.id} r={r} />)}
          </ul>
        </section>
      ))}
    </>
  );
}

function Requirement({ r }: { r: RequirementRow }) {
  const v = r.verdict ?? "unresolved";
  const cls = VERDICTS.find(([x]) => x === v)?.[1] ?? "";
  const gaps = Array.isArray(r.gaps) ? r.gaps : r.gaps ? [r.gaps] : [];
  return (
    <li className="card">
      <div className="row">
        <span className={`badge ${cls}`}>{v}</span>
        <Link href={entityHref(r.id)}><strong>{r.ref ?? r.id}</strong></Link>
        {r.confidence != null && <span className="small muted">confidence {Number(r.confidence).toFixed(2)}</span>}
        <span className="small muted">{r.tests} test{r.tests === 1 ? "" : "s"}</span>
      </div>
      <p style={{ margin: "6px 0" }}>{r.statement}</p>
      {r.evidence.length > 0 && (
        <details>
          <summary className="small">Evidence ({r.evidence.length})</summary>
          <ul className="small">
            {r.evidence.map((e) => (
              <li key={`${e.link}-${e.id}`}>
                <span className={`badge ${e.link === "CONTRADICTED_BY" ? "bad" : "ok"}`}>{e.link === "CONTRADICTED_BY" ? "contradicts" : "implements"}</span>{" "}
                <Link href={entityHref(e.id)}>{e.symbol}</Link> <span className="muted">{e.path}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
      {gaps.length > 0 && <p className="small muted">Gaps: {gaps.join("; ")}</p>}
    </li>
  );
}
