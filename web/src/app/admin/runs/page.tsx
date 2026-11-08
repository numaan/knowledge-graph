import Link from "next/link";
import { fmtDuration, fmtTime, parseLimit, LIMITS, statusClass } from "@/components/admin/format";
import { controlFetch } from "@/lib/control";
import { RUN_STATUSES, type Run, type Source } from "@/lib/control-types";

export const dynamic = "force-dynamic";

const STATUSES: readonly string[] = RUN_STATUSES;
type SP = Promise<Record<string, string | string[] | undefined>>;

const one = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v) ?? "";

export default async function RunsPage({ searchParams }: { searchParams: SP }) {
  const sp = await searchParams;
  const sourceId = one(sp.source_id);
  const status = STATUSES.includes(one(sp.status)) ? one(sp.status) : "";
  const limit = parseLimit(sp.limit);
  const q = new URLSearchParams();
  if (sourceId) q.set("source_id", sourceId);
  if (status) q.set("status", status);
  q.set("limit", String(limit));
  const [runsRes, srcRes] = await Promise.all([
    controlFetch<{ items: Run[] }>(`api/v1/runs?${q}`),
    controlFetch<{ items: Source[] }>("api/v1/sources"),
  ]);
  const sources = srcRes.ok ? srcRes.data.items : [];
  const runs = runsRes.ok ? runsRes.data.items : [];
  return (
    <>
      <h1>Runs</h1>
      <form method="get" className="row">
        <label>Source{" "}
          <select name="source_id" defaultValue={sourceId}>
            <option value="">All</option>
            {sources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </label>
        <label>Status{" "}
          <select name="status" defaultValue={status}>
            <option value="">All</option>
            {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label>Limit{" "}
          <select name="limit" defaultValue={String(limit)}>
            {LIMITS.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <button type="submit">Apply</button>
      </form>
      {!runsRes.ok ? (
        <p role="alert" className="error">Could not load runs: {runsRes.error}</p>
      ) : runs.length === 0 ? (
        <p className="notice">No runs match.</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <caption className="skip">Runs</caption>
            <thead>
              <tr>
                <th scope="col">Source</th><th scope="col">Kind</th><th scope="col">Status</th><th scope="col">Trigger</th>
                <th scope="col">Mode</th><th scope="col">Started</th><th scope="col">Duration</th><th scope="col">Detail</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <th scope="row">{r.source_name}</th>
                  <td>{r.kind}</td>
                  <td><span className={`badge ${statusClass(r.status)}`}>{r.status}</span></td>
                  <td>{r.trigger}</td>
                  <td>{r.mode}</td>
                  <td>{fmtTime(r.started_at)}</td>
                  <td>{fmtDuration(r.started_at, r.ended_at)}</td>
                  <td><Link href={`/admin/runs/${r.id}`}>Run {r.id.slice(0, 8)}</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
