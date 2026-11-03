"use client";

import { useId, useState } from "react";
import { FEEDBACK_KINDS, type FeedbackKind } from "@/lib/types";

const LABELS: Record<FeedbackKind, string> = {
  helpful: "Helpful",
  wrong: "Wrong",
  outdated: "Outdated",
  missing: "Something missing",
};

/** Feedback on an answer or an entity: helpful is sent at once; the others ask for an optional comment. */
export function FeedbackButtons({ nodeId, label = "Feedback" }: { nodeId: string; label?: string }) {
  const [kind, setKind] = useState<FeedbackKind | null>(null);
  const [comment, setComment] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "sent" | "error">("idle");
  const id = useId();

  async function send(k: FeedbackKind, text = "") {
    setState("sending");
    try {
      const res = await fetch("/api/kg/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ node_id: nodeId, kind: k, comment: text }),
      });
      setState(res.status === 202 ? "sent" : "error");
    } catch {
      setState("error");
    }
  }

  if (state === "sent") return <p className="feedback small muted" role="status">Thank you. Your feedback was sent.</p>;

  return (
    <div className="feedback" role="group" aria-label={label}>
      <span className="small muted">{label}:</span>
      {FEEDBACK_KINDS.map((k) => (
        <button key={k} type="button" aria-pressed={kind === k} disabled={state === "sending"}
          onClick={() => (k === "helpful" ? send(k) : setKind(kind === k ? null : k))}>
          {LABELS[k]}
        </button>
      ))}
      {kind && kind !== "helpful" && (
        <form onSubmit={(e) => { e.preventDefault(); send(kind, comment); }}>
          <label htmlFor={id} className="small">What is {kind === "missing" ? "missing" : kind}? (optional)</label>
          <textarea id={id} value={comment} maxLength={4000} onChange={(e) => setComment(e.target.value)} />
          <div className="row">
            <button type="submit" className="primary" disabled={state === "sending"}>Send feedback</button>
            <button type="button" onClick={() => setKind(null)}>Cancel</button>
          </div>
        </form>
      )}
      {state === "error" && <span className="small" role="alert" style={{ color: "var(--bad)" }}>Could not send feedback. Try again.</span>}
    </div>
  );
}
