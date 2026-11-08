import Link from "next/link";
import { EnabledToggle, SourceRowActions } from "@/components/admin/SourceRowActions";
import { fmtTime, statusClass } from "@/components/admin/format";
import { controlFetch } from "@/lib/control";
import type { Source } from "@/lib/control-types";

export const dynamic = "force-dynamic";

export default async function AdminPage() {
  const res = await controlFetch<{ items: Source[] }>("api/v1/sources");
  const items = res.ok ? res.data.items : [];
  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1>Sources</h1>
        <Link href="/admin/sources/new" className="button primary">Add source</Link>
      </div>
      {!res.ok ? (
        <p role="alert" className="error">Could not load sources: {res.error}</p>
      ) : items.length === 0 ? (
        <p className="notice">No sources yet. Add a source to index a repository, Jira, Confluence or S3.</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <caption className="skip">Sources</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Kind</th>
                <th scope="col">Last run</th>
                <th scope="col">Schedule</th>
                <th scope="col">Enabled</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.id}>
                  <th scope="row"><Link href={`/admin/sources/${s.id}`}>{s.name}</Link></th>
                  <td>{s.kind}</td>
                  <td>
                    {s.last_run ? (
                      <>
                        <span className={`badge ${statusClass(s.last_run.status)}`}>{s.last_run.status}</span>{" "}
                        <Link href={`/admin/runs/${s.last_run.id}`} className="small">{fmtTime(s.last_run.started_at ?? s.last_run.ended_at)}</Link>
                      </>
                    ) : (
                      <span className="muted">never run</span>
                    )}
                  </td>
                  <td>{s.schedule ? <code>{s.schedule}</code> : "manual"}</td>
                  <td><EnabledToggle id={s.id} name={s.name} enabled={s.enabled} /></td>
                  <td><SourceRowActions id={s.id} name={s.name} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
