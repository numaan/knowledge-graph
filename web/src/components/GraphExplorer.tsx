"use client";

import type { Core, ElementDefinition } from "cytoscape";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { entityHref, entityKind } from "@/lib/entity";
import type { Neighbor, NodeResponse } from "@/lib/types";

// Graph explorer: the entity and its neighbours. Click (or Enter on the list) expands a node,
// double-click opens its page, and the edge-type checkboxes hide or show edge types.

export interface GraphSeed {
  id: string;
  label: string;
  kind: string;
  neighbors: Neighbor[];
}

const short = (s: string, n = 32) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

export function toElements(centerId: string, neighbors: Neighbor[]): ElementDefinition[] {
  return neighbors.flatMap((n) => [
    { data: { id: n.id, label: short(n.name ?? n.id), kind: entityKind(n.labels) } },
    { data: { id: `${centerId}->${n.via?.[0] ?? "RELATED"}->${n.id}`, source: centerId, target: n.id, type: n.via?.[0] ?? "RELATED" } },
  ]);
}

function cssVar(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

export function GraphExplorer({ seed }: { seed: GraphSeed }) {
  const box = useRef<HTMLDivElement>(null);
  const cy = useRef<Core | null>(null);
  const expanded = useRef(new Set([seed.id]));
  const router = useRouter();
  const [types, setTypes] = useState<string[]>(() => [...new Set(seed.neighbors.map((n) => n.via?.[0] ?? "RELATED"))].sort());
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [status, setStatus] = useState("");

  const expand = useCallback(async (id: string) => {
    if (!cy.current || expanded.current.has(id)) return;
    expanded.current.add(id);
    setStatus(`Loading neighbours of ${id}…`);
    const res = await fetch(`/api/kg/node/${encodeURIComponent(id)}`);
    if (!res.ok) {
      setStatus(`Could not expand ${id}`);
      return;
    }
    const body = (await res.json()) as NodeResponse;
    const els = toElements(id, body.neighbors.results).filter((e) => cy.current!.getElementById(e.data.id!).empty());
    cy.current.add(els);
    cy.current.layout({ name: "cose", animate: false, randomize: false, fit: true, padding: 20 }).run();
    setTypes((t) => [...new Set([...t, ...body.neighbors.results.map((n) => n.via?.[0] ?? "RELATED")])].sort());
    setStatus(`Added ${els.length / 2} neighbours of ${id}`);
  }, []);

  useEffect(() => {
    let destroyed = false;
    (async () => {
      const cytoscape = (await import("cytoscape")).default;
      if (destroyed || !box.current) return;
      const text = cssVar("--text"), muted = cssVar("--muted"), accent = cssVar("--accent"), surface = cssVar("--surface-2");
      cy.current = cytoscape({
        container: box.current,
        elements: [{ data: { id: seed.id, label: short(seed.label), kind: seed.kind } }, ...toElements(seed.id, seed.neighbors)],
        layout: { name: "cose", animate: false, padding: 20 },
        wheelSensitivity: 0.3,
        style: [
          { selector: "node", style: { label: "data(label)", "font-size": 10, color: text, "background-color": surface,
            "border-width": 1, "border-color": muted, width: 18, height: 18, "text-valign": "bottom", "text-margin-y": 3 } },
          { selector: `node[id = ${JSON.stringify(seed.id)}]`, style: { "background-color": accent, width: 26, height: 26 } },
          { selector: "edge", style: { width: 1, "line-color": muted, "target-arrow-color": muted, "target-arrow-shape": "triangle",
            "curve-style": "bezier", label: "data(type)", "font-size": 7, color: muted, "text-rotation": "autorotate" } },
          { selector: ".hidden", style: { display: "none" } },
        ],
      });
      cy.current.on("tap", "node", (e) => expand(e.target.id()));
      cy.current.on("dbltap", "node", (e) => router.push(entityHref(e.target.id())));
    })();
    return () => {
      destroyed = true;
      cy.current?.destroy();
      cy.current = null;
    };
  }, [seed, expand, router]);

  useEffect(() => {
    cy.current?.edges().forEach((e) => {
      if (hidden.has(e.data("type"))) e.addClass("hidden");
      else e.removeClass("hidden");
    });
  }, [hidden, types]);

  const toggle = (t: string) => setHidden((h) => { const n = new Set(h); if (n.has(t)) n.delete(t); else n.add(t); return n; });
  const visible = useMemo(() => seed.neighbors.filter((n) => !hidden.has(n.via?.[0] ?? "RELATED")), [seed, hidden]);

  return (
    <section aria-labelledby="graph-h">
      <h2 id="graph-h">Graph explorer</h2>
      <fieldset className="filters">
        <legend className="small muted">Edge types</legend>
        {types.map((t) => (
          <label key={t}><input type="checkbox" checked={!hidden.has(t)} onChange={() => toggle(t)} /> {t}</label>
        ))}
      </fieldset>
      <div ref={box} className="graph" role="img"
        aria-label={`Graph of ${seed.label} and ${seed.neighbors.length} neighbours. Click a node to expand it, double-click to open it. The list below has the same nodes.`} />
      <p className="small muted" role="status" aria-live="polite">{status}</p>
      <details>
        <summary className="small">Expand from a list (keyboard)</summary>
        <ul className="small">
          {visible.slice(0, 50).map((n) => (
            <li key={n.id}>
              <button type="button" onClick={() => expand(n.id)}>Expand {n.name ?? n.id}</button>{" "}
              <a href={entityHref(n.id)}>Open</a>
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}
