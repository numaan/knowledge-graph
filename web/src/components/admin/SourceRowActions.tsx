"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { adminApi } from "@/lib/admin-client";
import { TestSourceButton } from "./TestSourceButton";

export function EnabledToggle({ id, name, enabled }: { id: string; name: string; enabled: boolean }) {
  const router = useRouter();
  const [value, setValue] = useState(enabled);
  const [error, setError] = useState("");

  async function toggle(next: boolean) {
    setError("");
    const res = await adminApi.patch(`sources/${id}`, { enabled: next });
    if (!res.ok) return setError(res.error);
    setValue(next);
    router.refresh();
  }

  return (
    <label className="row small">
      <input type="checkbox" checked={value} aria-label={`Enabled: ${name}`} onChange={(e) => toggle(e.target.checked)} />
      <span>{value ? "Enabled" : "Disabled"}</span>
      {error && <span role="alert" className="field-error">{error}</span>}
    </label>
  );
}

export function SourceRowActions({ id, name }: { id: string; name: string }) {
  const router = useRouter();
  const [mode, setMode] = useState<"incremental" | "full">("incremental");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState("");

  async function start(m: "incremental" | "full") {
    setConfirming(false);
    setBusy(true);
    const res = await adminApi.post(`sources/${id}/run`, { mode: m });
    setBusy(false);
    if (!res.ok) return setText(`Run failed: ${res.error}`);
    setText(`Run started (${m}).`);
    router.refresh();
  }

  return (
    <div className="row-actions">
      <div className="row">
        <select aria-label={`Mode for ${name}`} value={mode} onChange={(e) => setMode(e.target.value as "incremental" | "full")}>
          <option value="incremental">Incremental</option>
          <option value="full">Full</option>
        </select>
        <button type="button" disabled={busy} aria-label={`Run ${name}`}
          onClick={() => (mode === "full" ? setConfirming(true) : start(mode))}>Run</button>
        <TestSourceButton id={id} ariaLabel={`Test ${name}`} onText={setText} />
        <Link href={`/admin/sources/${id}`} className="button" aria-label={`Edit ${name}`}>Edit</Link>
      </div>
      {confirming && (
        <div role="alertdialog" aria-label={`Confirm full run of ${name}`} className="notice">
          <p>A full run reads all data of {name} again. It can take a long time. Continue?</p>
          <div className="row">
            <button type="button" className="primary" onClick={() => start("full")}>Confirm full run</button>
            <button type="button" onClick={() => setConfirming(false)}>Cancel</button>
          </div>
        </div>
      )}
      <span role="status" aria-live="polite" className="small">{text}</span>
    </div>
  );
}
