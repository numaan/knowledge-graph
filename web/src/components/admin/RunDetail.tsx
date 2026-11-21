"use client";

import { useEffect, useState } from "react";
import { adminApi } from "@/lib/admin-client";
import type { Run } from "@/lib/control-types";
import { fmtDuration, fmtTime, isActive, pollDelay, statusClass } from "./format";

export function RunDetail({ initial }: { initial: Run }) {
  const [run, setRun] = useState<Run>(initial);
  const [lost, setLost] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");
  const id = initial.id;
  const active = isActive(run.status);

  useEffect(() => {
    if (!active) return;
    let stopped = false;
    let errors = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    // One request at a time: the next timer starts only after the previous request ends.
    const tick = async () => {
      const res = await adminApi.get<Run>(`runs/${encodeURIComponent(id)}`);
      if (stopped) return;
      if (res.ok) {
        errors = 0;
        setLost(false);
        setRun(res.data);
        if (!isActive(res.data.status)) return;
      } else {
        errors += 1;
        if (errors >= 5) setLost(true);
      }
      timer = setTimeout(tick, pollDelay(errors));
    };
    timer = setTimeout(tick, pollDelay(0));
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [id, active]);

  async function cancel() {
    setError("");
    const res = await adminApi.post<Run>(`runs/${encodeURIComponent(id)}/cancel`);
    if (!res.ok) return setError(res.error);
    setConfirming(false);
    setRun(res.data);
  }

  const stats = Object.entries(run.stats ?? {});
  return (
    <>
      <h1>Run {run.id}</h1>
      <p>
        <span className={`badge ${statusClass(run.status)}`}>{run.status}</span>{" "}
        <span className="muted">{run.source_name} ({run.kind}), {run.mode}, {run.trigger}</span>
      </p>
      {lost && <p role="status" className="notice">Connection lost. Retrying every 10 seconds.</p>}
      <table className="data">
        <caption className="skip">Timing</caption>
        <tbody>
          <tr><th scope="row">Started</th><td>{fmtTime(run.started_at)}</td></tr>
          <tr><th scope="row">Ended</th><td>{fmtTime(run.ended_at)}</td></tr>
          <tr><th scope="row">Duration</th><td>{fmtDuration(run.started_at, run.ended_at)}</td></tr>
          <tr><th scope="row">Requested by</th><td>{run.requested_by ?? "-"}</td></tr>
        </tbody>
      </table>
      {run.dagster_url && <p><a href={run.dagster_url} target="_blank" rel="noreferrer noopener">Open in Dagster</a></p>}
      {active && (
        <div className="row">
          <button type="button" onClick={() => setConfirming(true)}>Cancel run</button>
        </div>
      )}
      {active && confirming && (
        <div role="alertdialog" aria-label="Confirm cancel of run" className="notice" style={{ marginTop: 8 }}>
          <p>Cancel this run? Dagster stops the run. Work already written stays.</p>
          <div className="row">
            <button type="button" className="primary" onClick={cancel}>Confirm cancel</button>
            <button type="button" onClick={() => setConfirming(false)}>Keep running</button>
          </div>
        </div>
      )}
      {error && <p role="alert" className="error">Could not cancel: {error}</p>}
      {run.error && <p className="error">{run.error}</p>}
      <h2>Stats</h2>
      {stats.length === 0 ? (
        <p className="muted">No stats yet.</p>
      ) : (
        <table className="data">
          <caption className="skip">Stats</caption>
          <tbody>
            {stats.map(([k, v]) => (
              <tr key={k}><th scope="row">{k}</th><td>{typeof v === "string" ? v : JSON.stringify(v)}</td></tr>
            ))}
          </tbody>
        </table>
      )}
      <h2>Log tail</h2>
      <pre aria-live="off" tabIndex={0} style={{ maxHeight: 360, overflow: "auto", whiteSpace: "pre-wrap" }}>
        {(run.log_tail ?? []).join("\n")}
      </pre>
    </>
  );
}
