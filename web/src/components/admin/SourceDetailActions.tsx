"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { adminApi } from "@/lib/admin-client";
import { TestSourceButton } from "./TestSourceButton";

export function SourceDetailActions({ id, name }: { id: string; name: string }) {
  const router = useRouter();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");

  async function remove() {
    setError("");
    const res = await adminApi.del(`sources/${id}`);
    if (!res.ok) return setError(res.error);
    router.push("/admin");
    router.refresh();
  }

  return (
    <div className="card">
      <div className="row">
        <TestSourceButton id={id} label="Test connection" />
      </div>
      <div className="row" style={{ marginTop: 12 }}>
        <button type="button" onClick={() => setConfirming(true)}>Delete source</button>
      </div>
      {confirming && (
        <div role="alertdialog" aria-label={`Confirm delete of ${name}`} className="notice" style={{ marginTop: 8 }}>
          <p>Delete {name}? Graph data stays: nodes and documents already built from this source are not removed. Scheduled and manual runs stop.</p>
          <div className="row">
            <button type="button" className="primary" onClick={remove}>Confirm delete</button>
            <button type="button" onClick={() => setConfirming(false)}>Cancel</button>
          </div>
        </div>
      )}
      {error && <p role="alert" className="error">Could not delete: {error}</p>}
    </div>
  );
}
