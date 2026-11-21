import Link from "next/link";
import { notFound } from "next/navigation";
import { SourceDetailActions } from "@/components/admin/SourceDetailActions";
import { SourceForm } from "@/components/admin/SourceForm";
import { fmtTime, statusClass } from "@/components/admin/format";
import { controlFetch } from "@/lib/control";
import type { Run, Source } from "@/lib/control-types";

export const dynamic = "force-dynamic";

export default async function EditSourcePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const [res, runsRes] = await Promise.all([
    controlFetch<Source>(`api/v1/sources/${encodeURIComponent(id)}`),
    controlFetch<{ items: Run[] }>(`api/v1/runs?source_id=${encodeURIComponent(id)}&limit=10`),
  ]);
  if (!res.ok && res.status === 404) notFound();
  if (!res.ok) return <p role="alert" className="error">Could not load the source: {res.error}</p>;
  const source = res.data;
  const runs = runsRes.ok ? runsRes.data.items : [];
  return (
    <>
      <h1>{source.name}</h1>
      <p className="muted">{source.kind} source{source.origin ? `, origin: ${source.origin}` : ""}</p>
      <SourceForm source={source} />
      <h2>Test and delete</h2>
      <SourceDetailActions id={source.id} name={source.name} />
      <h2>Recent runs</h2>
      {!runsRes.ok ? (
        <p role="alert" className="error">Could not load runs: {runsRes.error}</p>
      ) : runs.length === 0 ? (
        <p className="muted">No runs yet.</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr><th scope="col">Run</th><th scope="col">Status</th><th scope="col">Mode</th><th scope="col">Trigger</th><th scope="col">Started</th></tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <th scope="row"><Link href={`/admin/runs/${r.id}`}>Run {r.id.slice(0, 8)}</Link></th>
                  <td><span className={`badge ${statusClass(r.status)}`}>{r.status}</span></td>
                  <td>{r.mode}</td>
                  <td>{r.trigger}</td>
                  <td>{fmtTime(r.started_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
