"use client";

import { useState } from "react";
import { adminApi } from "@/lib/admin-client";
import type { TestResult } from "@/lib/control-types";

/** Runs POST /sources/{id}/test and shows the result as text in a live region. */
export function TestSourceButton({ id, label = "Test", ariaLabel, onText }: { id: string; label?: string; ariaLabel?: string; onText?: (t: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [own, setOwn] = useState("");
  const setText = onText ?? setOwn;

  async function test() {
    setBusy(true);
    setText("Testing...");
    const res = await adminApi.post<TestResult>(`sources/${id}/test`);
    setBusy(false);
    if (!res.ok) setText(`Failed: ${res.error}`);
    else setText(res.data.ok ? `OK: ${res.data.message}` : `Failed: ${res.data.message}`);
  }

  return (
    <>
      <button type="button" onClick={test} disabled={busy} aria-label={ariaLabel}>{label}</button>
      {!onText && <span role="status" aria-live="polite" className="small">{own}</span>}
    </>
  );
}
