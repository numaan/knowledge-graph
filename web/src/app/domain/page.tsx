import type { Metadata } from "next";
import Link from "next/link";
import { ToolError } from "@/components/ToolError";
import { callTool } from "@/lib/kg";
import type { Props } from "@/lib/types";

export const metadata: Metadata = { title: "Domain overview" };

type SP = Promise<{ repo?: string }>;
type Doc = { data: Props };
const list = (v: unknown) => (Array.isArray(v) ? (v as Props[]) : []);

export default async function DomainPage({ searchParams }: { searchParams: SP }) {
  const { repo = "" } = await searchParams;
  const repos = await callTool<Doc>("domain_overview", {});
  const names = repos.ok ? list(repos.data.data.repositories).map((r) => String(r.repo)) : [];
  const current = repo || (names.length === 1 ? names[0] : "");
  const ov = current ? await callTool<Doc>("domain_overview", { repo: current }) : undefined;

  return (
    <>
      <h1>Domain overview</h1>
      {!repos.ok && <ToolError r={repos} what="Domain overview" />}
      {names.length > 0 && (
        <nav className="tabs" aria-label="Repository">
          {names.map((n) => (
            <Link key={n} href={`/domain?repo=${encodeURIComponent(n)}`} aria-current={n === current ? "true" : undefined}>{n}</Link>
          ))}
        </nav>
      )}
      {repos.ok && names.length === 0 && <p className="muted">No repositories are ingested yet.</p>}
      {!current && names.length > 1 && <p className="muted">Select a repository.</p>}
      {ov && !ov.ok && <ToolError r={ov} what="Domain overview" />}
      {ov?.ok && <Overview data={ov.data.data} />}
    </>
  );
}

function Overview({ data }: { data: Props }) {
  const caps = list(data.capabilities), concepts = list(data.concepts), rules = list(data.rules);
  const entities = list(data.entities), endpoints = list(data.endpoints);
  const byModule = new Map<string, Props[]>();
  for (const r of rules) byModule.set(String(r.module ?? ""), [...(byModule.get(String(r.module ?? "")) ?? []), r]);
  return (
    <>
      <div className="stats" role="list" aria-label="Counts">
        {[["Capabilities", caps], ["Concepts", concepts], ["Business rules", rules], ["Entities", entities], ["Endpoints", endpoints]].map(([l, v]) => (
          <div key={l as string} className="stat" role="listitem"><strong>{(v as Props[]).length}</strong>{l as string}</div>
        ))}
      </div>
      <h2>Capabilities</h2>
      <ul className="list">
        {caps.map((c, i) => (
          <li key={i} className="card"><strong>{String(c.name)}</strong> <span className="small muted">{String(c.module ?? "")}</span>
            {c.summary ? <p className="small">{String(c.summary)}</p> : null}</li>
        ))}
      </ul>
      <h2>Domain concepts</h2>
      <div className="table-wrap">
        <table className="data">
          <thead><tr><th scope="col">Concept</th><th scope="col">Kind</th><th scope="col">Symbols</th><th scope="col">Definition</th></tr></thead>
          <tbody>
            {concepts.map((c, i) => (
              <tr key={i}><td><Link href={`/search?q=${encodeURIComponent(String(c.name))}`}>{String(c.name)}</Link></td>
                <td>{String(c.kind ?? "")}</td><td>{String(c.symbols ?? 0)}</td><td className="small">{String(c.definition ?? "")}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>Business rules</h2>
      {[...byModule.entries()].map(([m, rs]) => (
        <details key={m} open={byModule.size <= 4}>
          <summary><strong>{m || "(no module)"}</strong> <span className="small muted">({rs.length})</span></summary>
          <ul className="small">{rs.map((r, i) => <li key={i}>{String(r.statement)}</li>)}</ul>
        </details>
      ))}
      <div className="grid-2">
        <section>
          <h2>Entities</h2>
          <ul className="small">{entities.map((e, i) => <li key={i}>{String(e.entity)} <span className="muted">{String(e.path)}</span></li>)}</ul>
        </section>
        <section>
          <h2>HTTP endpoints</h2>
          <ul className="small">
            {endpoints.map((e, i) => <li key={i}><code>{String(e.method ?? "")} {String(e.route ?? "")}</code> <span className="muted">{String(e.handler)}</span></li>)}
          </ul>
        </section>
      </div>
    </>
  );
}
