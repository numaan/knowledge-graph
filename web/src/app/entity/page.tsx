import type { Metadata } from "next";
import Link from "next/link";
import { FeedbackButtons } from "@/components/FeedbackButtons";
import { GraphExplorer } from "@/components/GraphExplorer";
import { ToolError } from "@/components/ToolError";
import {
  badges, displayProps, entityHref, entityKind, entityTitle, groupNeighbors, languageOf, LONG_TEXT, sourceLink,
} from "@/lib/entity";
import { highlight } from "@/lib/highlight";
import { callTool, getNode } from "@/lib/kg";
import type { Props } from "@/lib/types";

type SP = Promise<{ id?: string }>;

export async function generateMetadata({ searchParams }: { searchParams: SP }): Promise<Metadata> {
  const { id = "" } = await searchParams;
  return { title: id.split(/[#/:]/).pop() || "Entity" };
}

type Rows = { results: Props[]; note?: string };
type Doc = { data: Props };

export default async function EntityPage({ searchParams }: { searchParams: SP }) {
  const { id = "" } = await searchParams;
  if (!id) return <p className="error">No entity id. Open an entity from <Link href="/search">Search</Link>.</p>;
  const res = await getNode(id);
  if (!res.ok) {
    return (
      <>
        <h1>Entity not available</h1>
        {res.status === 404 ? <p className="notice">No node <code>{id}</code>, or you may not see it.</p> : <p className="error" role="alert">{res.error}</p>}
      </>
    );
  }
  const { node, neighbors } = res.data;
  const p = node.properties;
  const kind = entityKind(node.labels);
  const title = entityTitle(node);
  const rows = neighbors.results ?? [];
  const b = badges(p, rows);
  const link = sourceLink(p);

  const [code, history, trace, ticket] = await Promise.all([
    kind === "Symbol" ? callTool<Doc>("get_symbol_code", { symbol: id }) : undefined,
    kind === "Symbol" || kind === "File"
      ? callTool<Rows>("get_change_history", { symbol: kind === "File" ? String(p.path) : id, repo: p.repo ?? "", limit: 10 })
      : undefined,
    kind === "Requirement" ? callTool<Doc>("trace_requirement", { requirement: id }) : undefined,
    kind === "Ticket" && p.key ? callTool<Doc>("get_context_for_ticket", { key: String(p.key) }) : undefined,
  ]);
  const sym = code?.ok ? code.data.data : undefined;
  const codeHtml = typeof sym?.code === "string"
    ? await highlight(sym.code.replace(/\n+$/, ""), languageOf(p.path), Number(sym.start_line ?? 1))
    : undefined;
  const longText = LONG_TEXT.map((k) => [k, p[k]] as const).filter(([, v]) => typeof v === "string" && v.length > 160);

  return (
    <article>
      <div className="row small">
        <span className="badge">{kind}</span>
        {node.labels.filter((l) => l !== kind).map((l) => <span key={l} className="badge">{l}</span>)}
        {b.stale && <span className="badge warn" title="The source changed or was not seen in the last sync">stale</span>}
        {b.conflict && <span className="badge bad" title="Sources or code contradict this">conflict</span>}
      </div>
      <h1>{title}</h1>
      <p className="small muted" style={{ overflowWrap: "anywhere" }}>
        <code>{id}</code>
        {typeof p.path === "string" && <> · {p.path}{p.start_line ? `:${p.start_line}` : ""}</>}
        {link && <> · <a href={link} rel="noreferrer noopener" target="_blank">Open source</a></>}
      </p>

      {longText.map(([k, v]) => (
        <section key={k} aria-label={k}>
          <h2>{k[0].toUpperCase() + k.slice(1)}</h2>
          <div className="card longtext">{String(v)}</div>
        </section>
      ))}

      {codeHtml && (
        <section aria-labelledby="code-h">
          <h2 id="code-h">Code</h2>
          {typeof sym?.signature === "string" && <p className="small muted"><code>{sym.signature}</code></p>}
          <div className="code" dangerouslySetInnerHTML={{ __html: codeHtml }} />
        </section>
      )}
      {code && !code.ok && <ToolError r={code} what="Code view" />}

      {trace && (trace.ok ? <RequirementTrace data={trace.data.data} /> : <ToolError r={trace} what="Requirement trace" />)}
      {ticket && ticket.ok && <TicketContext data={ticket.data.data} />}

      <div className="grid-2">
        <section aria-labelledby="props-h">
          <h2 id="props-h">Properties</h2>
          <div className="table-wrap">
            <table className="props">
              <tbody>
                {displayProps(p).map(([k, v]) => (
                  <tr key={k}><th scope="row">{k}</th><td>{v}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
        <section aria-labelledby="nb-h">
          <h2 id="nb-h">Neighbours ({rows.length})</h2>
          {rows.length === 0 && <p className="muted">No neighbours.</p>}
          {groupNeighbors(rows).map(([type, list]) => (
            <details key={type} open={list.length <= 12}>
              <summary><strong>{type}</strong> <span className="muted small">({list.length})</span></summary>
              <ul className="small">
                {list.map((n) => (
                  <li key={n.id}>
                    <Link href={entityHref(n.id)}>{n.name ?? n.id}</Link>{" "}
                    <span className="muted">{entityKind(n.labels)}{n.distance > 1 ? ` · ${n.distance} hops` : ""}</span>
                  </li>
                ))}
              </ul>
            </details>
          ))}
        </section>
      </div>

      {history && (
        <section aria-labelledby="hist-h">
          <h2 id="hist-h">Change history</h2>
          {!history.ok ? <ToolError r={history} what="Change history" /> : history.data.results.length === 0 ? (
            <p className="muted">{history.data.note ?? "No commits."}</p>
          ) : (
            <div className="table-wrap">
              <table className="data">
                <thead><tr><th scope="col">Date</th><th scope="col">Author</th><th scope="col">Commit</th><th scope="col">Tickets</th></tr></thead>
                <tbody>
                  {history.data.results.map((c) => (
                    <tr key={String(c.sha)}>
                      <td>{String(c.date ?? "").slice(0, 10)}</td>
                      <td>{String(c.author ?? "")}</td>
                      <td><Link href={entityHref(`git:${p.repo}:${c.sha}`)}>{String(c.title ?? c.sha)}</Link></td>
                      <td>{Array.isArray(c.tickets) ? c.tickets.join("; ") : ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      <GraphExplorer seed={{ id, label: title, kind, neighbors: rows }} />
      <FeedbackButtons nodeId={id} label="Is this page right?" />
    </article>
  );
}

const VERDICT: Record<string, string> = { supported: "ok", contradicted: "bad", unresolved: "warn" };

function RequirementTrace({ data }: { data: Props }) {
  const evidence = (Array.isArray(data.evidence) ? data.evidence : []) as Props[];
  const gaps = Array.isArray(data.gaps) ? data.gaps : data.gaps ? [data.gaps] : [];
  const v = String(data.verdict ?? "unresolved");
  return (
    <section aria-labelledby="trace-h">
      <h2 id="trace-h">Verdict <span className={`badge ${VERDICT[v] ?? ""}`}>{v}</span></h2>
      {data.reasoning ? <p className="longtext">{String(data.reasoning)}</p> : null}
      {gaps.length > 0 && (<><h3>Gaps</h3><ul>{gaps.map((g, i) => <li key={i}>{String(g)}</li>)}</ul></>)}
      <h3>Evidence</h3>
      {evidence.length === 0 ? <p className="muted">No linked code.</p> : (
        <ul>
          {evidence.map((e, i) => (
            <li key={i}>
              <span className="badge">{String(e.link)}</span>{" "}
              <Link href={entityHref(String(e.symbol_id))}>{String(e.symbol)}</Link>
              {e.explanation ? <div className="small muted">{String(e.explanation)}</div> : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function TicketContext({ data }: { data: Props }) {
  const commits = (data.commits ?? []) as Props[];
  const links = (data.links ?? []) as Props[];
  if (commits.length + links.length === 0) return null;
  return (
    <section aria-labelledby="tc-h">
      <h2 id="tc-h">Work on this ticket</h2>
      {links.length > 0 && (
        <ul className="small">
          {links.map((l, i) => (
            <li key={i}>{String(l.link)}: <Link href={entityHref(`jira:${l.key}`)}>{String(l.key)}</Link> {String(l.title ?? "")}</li>
          ))}
        </ul>
      )}
      {commits.length > 0 && (
        <ul className="small">
          {commits.map((c) => (
            <li key={String(c.sha)}>
              <Link href={entityHref(`git:${c.repo}:${c.sha}`)}>{String(c.sha).slice(0, 8)}</Link> {String(c.title ?? "")}
              <span className="muted"> · {String(c.author ?? "")}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
