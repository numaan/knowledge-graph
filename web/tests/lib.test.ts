import { normaliseAnswer, splitCitations } from "@/lib/answer";
import { badges, breadcrumb, displayProps, entityHref, entityKind, entityTitle, groupNeighbors, languageOf, sourceLink } from "@/lib/entity";
import type { Neighbor } from "@/lib/types";

describe("normaliseAnswer", () => {
  it("reads {data: {answer, citations}}", () => {
    const a = normaliseAnswer({ data: { answer: "x [1]", citations: [{ id: "a", title: "A" }], confidence: 0.5 } });
    expect(a).toEqual({ answer: "x [1]", citations: [{ n: 1, id: "a", title: "A", url: undefined, kind: undefined, snippet: undefined }], confidence: 0.5, note: undefined });
  });
  it("reads a flat result with sources and names", () => {
    const a = normaliseAnswer({ answer: "y", sources: [{ n: 4, name: "B", url: "https://x" }] });
    expect(a.citations[0]).toMatchObject({ n: 4, title: "B", url: "https://x" });
  });
  it("tolerates empty input", () => {
    expect(normaliseAnswer(null)).toMatchObject({ answer: "", citations: [] });
  });
});

describe("splitCitations", () => {
  const known = new Set([1, 2, 3]);
  it("splits markers, including lists", () => {
    expect(splitCitations("a [1] b [2, 3].", known)).toEqual([{ text: "a " }, { cite: 1 }, { text: " b " }, { cite: 2 }, { cite: 3 }, { text: "." }]);
  });
  it("keeps unknown numbers and array indexes as text", () => {
    expect(splitCitations("xs[0] and [9]", known)).toEqual([{ text: "xs[0] and [9]" }]);
  });
});

describe("entity helpers", () => {
  it("picks the kind from labels", () => {
    expect(entityKind(["Code", "Symbol"])).toBe("Symbol");
    expect(entityKind(["Source", "Person"])).toBe("Person");
    expect(entityKind(["Source", "Gadget"])).toBe("Gadget");
    expect(entityKind([])).toBe("Node");
  });
  it("builds titles, hrefs and source links", () => {
    expect(entityTitle({ labels: [], properties: { id: "x", key: "PREC-1", title: "T" } })).toBe("T");
    expect(entityHref("precept:a/b.py#f")).toBe("/entity?id=precept%3Aa%2Fb.py%23f");
    expect(sourceLink({ url: "javascript:alert(1)" })).toBeUndefined();
    expect(sourceLink({ html_url: "https://github.com/x" })).toBe("https://github.com/x");
  });
  it("sets stale and conflict badges", () => {
    const nb: Neighbor[] = [{ id: "s", labels: ["Symbol"], name: "s", distance: 1, via: ["CONTRADICTED_BY"] }];
    expect(badges({ stale: true })).toEqual({ stale: true, conflict: false });
    expect(badges({}, nb)).toEqual({ stale: false, conflict: true });
    expect(badges({ conflicts: ["doc:1"] }).conflict).toBe(true);
  });
  it("groups neighbours by edge type, largest first", () => {
    const n = (id: string, t: string): Neighbor => ({ id, labels: [], name: id, distance: 1, via: [t] });
    const g = groupNeighbors([n("a", "DEFINES"), n("b", "CALLS"), n("c", "CALLS")]);
    expect(g.map(([t, l]) => [t, l.length])).toEqual([["CALLS", 2], ["DEFINES", 1]]);
  });
  it("hides internal and long properties", () => {
    const rows = displayProps({ id: "x", content_hash: "h", acl: ["a"], text: "t", tags: ["a", "b"], empty: [], description: "d".repeat(200), n: 3 });
    expect(rows).toEqual([["n", "3"], ["tags", "a, b"]]);
  });
  it("collapses repeated breadcrumb parts", () => {
    expect(breadcrumb("Doc > Doc > Design > Scope")).toBe("Doc > Design > Scope");
    expect(breadcrumb("A > B > A")).toBe("A > B > A");
  });
  it("maps file extensions to languages", () => {
    expect(languageOf("a/B.java")).toBe("java");
    expect(languageOf("x.unknown")).toBe("text");
    expect(languageOf(undefined)).toBe("text");
  });
});
