import type { Metadata } from "next";
import Link from "next/link";
import { SearchForm } from "@/components/SearchForm";
import { ToolError } from "@/components/ToolError";
import { breadcrumb, entityHref } from "@/lib/entity";
import { callTool } from "@/lib/kg";
import type { CodeHit, KnowledgeHit, Result } from "@/lib/types";

export const metadata: Metadata = { title: "Search" };

const TYPES = [
  ["", "All"],
  ["code", "Code"],
  ["Ticket", "Tickets"],
  ["Section", "Documents"],
] as const;

type SP = Promise<{ q?: string; type?: string; repo?: string }>;

export default async function SearchPage({ searchParams }: { searchParams: SP }) {
  const { q = "", type = "", repo = "" } = await searchParams;
  const query = q.trim();
  let code: Result<{ results: CodeHit[] }> | undefined;
  let knowledge: Result<{ results: KnowledgeHit[] }> | undefined;
  if (query) {
    [code, knowledge] = await Promise.all([
      type === "" || type === "code" ? callTool<{ results: CodeHit[] }>("search_graph", { query, repo, k: 10 }) : undefined,
      type !== "code" ? callTool<{ results: KnowledgeHit[] }>("search_knowledge", { query, kind: type, k: 10 }) : undefined,
    ]);
  }
  const href = (t: string) => `/search?${new URLSearchParams({ q: query, ...(t && { type: t }), ...(repo && { repo }) })}`;

  return (
    <>
      <h1>Search</h1>
      <SearchForm q={query} type={type} />
      <nav className="tabs" aria-label="Result type">
        {TYPES.map(([t, label]) => (
          <Link key={t} href={href(t)} aria-current={t === type ? "true" : undefined}>{label}</Link>
        ))}
      </nav>
      {!query && <p className="muted">Type a question or a name. Results come from code search and from tickets and documents.</p>}
      {code && (
        <section aria-labelledby="code-h">
          <h2 id="code-h">Code</h2>
          {code.ok ? <CodeResults rows={code.data.results} /> : <ToolError r={code} what="Code search" />}
        </section>
      )}
      {knowledge && (
        <section aria-labelledby="know-h">
          <h2 id="know-h">Tickets and documents</h2>
          {knowledge.ok ? <KnowledgeResults rows={knowledge.data.results} /> : <ToolError r={knowledge} what="Knowledge search" />}
        </section>
      )}
    </>
  );
}

function CodeResults({ rows }: { rows: CodeHit[] }) {
  if (rows.length === 0) return <p className="muted">No code matches.</p>;
  return (
    <ol className="list">
      {rows.map((r, i) => (
        <li key={`${r.symbol_id ?? r.path}-${i}`} className="card result">
          <h3>
            {r.symbol_id ? <Link href={entityHref(r.symbol_id)}>{r.symbol}</Link> : r.symbol}{" "}
            <span className="badge">{r.kind}</span> {r.role && <span className="badge">{r.role}</span>}
          </h3>
          <div className="small muted">{r.path}{r.start_line ? `:${r.start_line}` : ""}{r.route ? ` · ${r.route}` : ""}</div>
          {r.concepts && r.concepts.length > 0 && <p className="small">Concepts: {r.concepts.join(", ")}</p>}
          {r.code && <pre className="excerpt">{r.code}</pre>}
        </li>
      ))}
    </ol>
  );
}

function KnowledgeResults({ rows }: { rows: KnowledgeHit[] }) {
  if (rows.length === 0) return <p className="muted">No tickets or documents match.</p>;
  return (
    <ol className="list">
      {rows.map((r) => (
        <li key={r.id} className="card result">
          <h3>
            <Link href={entityHref(r.id)}>{r.key ? `${r.key}: ` : ""}{breadcrumb(r.title)}</Link>{" "}
            <span className="badge">{r.kind === "Section" ? "Document" : r.kind}</span>
            {r.status && <> <span className="badge">{r.status}</span></>}
          </h3>
          {r.text && <p className="small muted">{r.text.slice(0, 280)}{r.text.length > 280 ? "…" : ""}</p>}
          {r.url && <a className="small" href={r.url} rel="noreferrer noopener" target="_blank">Open source</a>}
        </li>
      ))}
    </ol>
  );
}
