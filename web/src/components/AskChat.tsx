"use client";

import Link from "next/link";
import { Fragment, useRef, useState } from "react";
import { normaliseAnswer, splitCitations } from "@/lib/answer";
import { entityHref } from "@/lib/entity";
import type { Answer, Citation } from "@/lib/types";
import { FeedbackButtons } from "./FeedbackButtons";

type Turn =
  | { role: "user"; text: string }
  | { role: "answer"; answer: Answer; question: string }
  | { role: "error"; text: string; notConfigured?: boolean };

export function AskChat() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLTextAreaElement>(null);

  async function ask(e: React.FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || busy) return;
    setTurns((t) => [...t, { role: "user", text: q }]);
    setQuestion("");
    setBusy(true);
    let turn: Turn;
    try {
      const res = await fetch("/api/kg/tools/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.status === 404) turn = { role: "error", notConfigured: true, text: "The ask tool is not configured on this server." };
      // Session gone (e.g. the access token could not be refreshed); reloading sends the user to sign-in.
      else if (res.status === 401) turn = { role: "error", text: "Your session has expired. Reload the page to sign in again." };
      else if (!res.ok) turn = { role: "error", text: body.error ?? `Request failed (${res.status})` };
      else turn = { role: "answer", answer: normaliseAnswer(body), question: q };
    } catch (err) {
      turn = { role: "error", text: `Could not reach the server: ${(err as Error).message}` };
    }
    setTurns((t) => [...t, turn]);
    setBusy(false);
    input.current?.focus();
  }

  return (
    <div className="chat">
      <div aria-live="polite" className="chat">
        {turns.map((t, i) =>
          t.role === "user" ? (
            <div key={i} className="msg-user"><span className="skip">You asked: </span>{t.text}</div>
          ) : t.role === "error" ? (
            <div key={i} className={t.notConfigured ? "notice" : "error"} role={t.notConfigured ? undefined : "alert"}>
              {t.text}
              {t.notConfigured && <> Use <Link href="/search">Search</Link> instead.</>}
            </div>
          ) : (
            <AnswerView key={i} answer={t.answer} question={t.question} />
          ),
        )}
        {busy && <p className="muted" role="status">Looking for an answer…</p>}
      </div>
      <form onSubmit={ask} className="card" style={{ display: "grid", gap: 8 }}>
        <label htmlFor="question">Your question</label>
        <textarea id="question" ref={input} value={question} onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. Why are scenarios not saved in local storage?"
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) ask(e); }} />
        <div className="row">
          <button type="submit" className="primary" disabled={busy || !question.trim()}>Ask</button>
          <span className="small muted">Enter sends, Shift+Enter adds a line.</span>
        </div>
      </form>
    </div>
  );
}

function citeHref(c: Citation) {
  return c.id ? entityHref(c.id) : c.url;
}

export function AnswerView({ answer, question }: { answer: Answer; question: string }) {
  const known = new Set(answer.citations.map((c) => c.n));
  const byN = new Map(answer.citations.map((c) => [c.n, c]));
  const paragraphs = answer.answer.split(/\n{2,}/);
  return (
    <article className="card msg-answer" aria-label={`Answer to: ${question}`}>
      {answer.answer ? (
        paragraphs.map((p, i) => (
          <p key={i} className="longtext">
            {splitCitations(p, known).map((s, j) =>
              "text" in s ? (
                <Fragment key={j}>{s.text}</Fragment>
              ) : (
                <a key={j} className="cite" href={citeHref(byN.get(s.cite)!) ?? `#cite-${s.cite}`}
                  aria-label={`Source ${s.cite}: ${byN.get(s.cite)?.title}`}>[{s.cite}]</a>
              ),
            )}
          </p>
        ))
      ) : (
        <p className="muted">No answer was found.</p>
      )}
      {answer.note && <p className="small muted">{answer.note}</p>}
      {answer.citations.length > 0 && (
        <>
          <h3>Sources</h3>
          <ol className="citations">
            {answer.citations.map((c) => (
              <li key={c.n} id={`cite-${c.n}`} value={c.n}>
                {c.id ? <Link href={entityHref(c.id)}>{c.title}</Link> : c.url ? <a href={c.url} rel="noreferrer noopener">{c.title}</a> : c.title}
                {c.kind && <> <span className="badge">{c.kind}</span></>}
                {c.snippet && <div className="small muted">{c.snippet}</div>}
              </li>
            ))}
          </ol>
        </>
      )}
      <FeedbackButtons nodeId={`answer:${question.slice(0, 500)}`} label="Was this answer right?" />
    </article>
  );
}
