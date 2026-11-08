import { fmtTime, kvText, LIMITS, parseLimit } from "@/components/admin/format";
import { controlFetch } from "@/lib/control";
import type { AuditEntry } from "@/lib/control-types";

export const dynamic = "force-dynamic";

export default async function AuditPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const limit = parseLimit((await searchParams).limit);
  const res = await controlFetch<{ items: AuditEntry[] }>(`api/v1/audit?limit=${limit}`);
  const items = res.ok ? res.data.items : [];
  return (
    <>
      <h1>Audit</h1>
      <form method="get" className="row">
        <label>Limit{" "}
          <select name="limit" defaultValue={String(limit)}>
            {LIMITS.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <button type="submit">Apply</button>
      </form>
      {!res.ok ? (
        <p role="alert" className="error">Could not load the audit log: {res.error}</p>
      ) : items.length === 0 ? (
        <p className="notice">No audit entries yet.</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <caption className="skip">Audit log</caption>
            <thead>
              <tr><th scope="col">Time</th><th scope="col">Actor</th><th scope="col">Action</th><th scope="col">Target</th><th scope="col">Detail</th></tr>
            </thead>
            <tbody>
              {items.map((e) => (
                <tr key={e.id}>
                  <td>{fmtTime(e.at)}</td>
                  <td>{e.actor}</td>
                  <td>{e.action}</td>
                  <td>{e.target ?? "-"}</td>
                  <td><code>{kvText(e.detail)}</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
