// Contract test: the JSON that the real control API produced (pipeline/tests/test_control_contract.py
// writes tests/fixtures/control/*.json) must match web/src/lib/control-types.ts. A field or enum that drifts fails here.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  RUN_MODES, RUN_STATUSES, SOURCE_KINDS,
  type ApiError, type AuditEntry, type MeResult, type Run, type Source, type TestResult,
} from "@/lib/control-types";

const DIR = join(__dirname, "fixtures", "control");
const load = (name: string): unknown => JSON.parse(readFileSync(join(DIR, `${name}.json`), "utf8"));

type Check = (v: unknown) => boolean;
const str: Check = (v) => typeof v === "string";
const bool: Check = (v) => typeof v === "boolean";
const nullable = (c: Check): Check => (v) => v === null || c(v);
const oneOf = (values: readonly string[]): Check => (v) => typeof v === "string" && values.includes(v);
const obj: Check = (v) => typeof v === "object" && v !== null && !Array.isArray(v);
const strings: Check = (v) => Array.isArray(v) && v.every(str);
const iso: Check = (v) => typeof v === "string" && !Number.isNaN(Date.parse(v));

// A shape lists a check for EVERY key of the type (the mapped type makes tsc fail when a key is missing or extra).
type Shape<T> = { [K in keyof Required<T>]: Check };
function conforms<T>(shape: Shape<T>, value: unknown, what: string, optional: string[] = []): string[] {
  if (!obj(value)) return [`${what}: not an object`];
  const rec = value as Record<string, unknown>;
  const problems: string[] = [];
  for (const [k, check] of Object.entries<Check>(shape as Record<string, Check>)) {
    if (!(k in rec)) {
      if (!optional.includes(k)) problems.push(`${what}.${k}: missing`);
    } else if (!check(rec[k])) problems.push(`${what}.${k}: bad value ${JSON.stringify(rec[k])}`);
  }
  for (const k of Object.keys(rec)) if (!(k in shape)) problems.push(`${what}.${k}: not in the type`);
  return problems;
}

const runShape: Shape<Run> = {
  id: str, source_id: nullable(str), source_name: str, kind: oneOf(SOURCE_KINDS), status: oneOf(RUN_STATUSES),
  trigger: str, mode: oneOf(RUN_MODES), dagster_run_id: nullable(str), dagster_url: nullable(str),
  stats: nullable(obj), error: nullable(str), started_at: nullable(iso), ended_at: nullable(iso),
  requested_by: nullable(str), log_tail: strings,
};
const sourceShape: Shape<Source> = {
  id: str, kind: oneOf(SOURCE_KINDS), name: str, enabled: bool, schedule: nullable(str), config: obj,
  has_secret: bool, secret_keys: strings, origin: str, last_run: nullable((v) => conforms(runShape, v, "last_run", ["log_tail"]).length === 0),
  created_at: iso, updated_at: iso,
};
const auditShape: Shape<AuditEntry> = { id: str, at: nullable(iso), actor: str, action: str, target: nullable(str), detail: nullable(obj) };
const meShape: Shape<MeResult> = { user: str, is_admin: bool };
const testShape: Shape<TestResult> = { ok: bool, message: str, details: obj };
const errorShape: Shape<ApiError> = { error: str };

const run = (v: unknown, what: string) => conforms(runShape, v, what, ["log_tail"]);
const items = (v: unknown, each: (x: unknown, w: string) => string[], what: string): string[] =>
  obj(v) && Array.isArray((v as { items?: unknown }).items) && Object.keys(v as object).length === 1
    ? (v as { items: unknown[] }).items.flatMap((x, i) => each(x, `${what}[${i}]`))
    : [`${what}: expected { items: [...] }`];

// Every fixture must be listed here: a new fixture without a check fails the last test.
const checks: Record<string, (v: unknown) => string[]> = {
  me: (v) => conforms(meShape, v, "me"),
  source_create: (v) => conforms(sourceShape, v, "source"),
  source_create_jira: (v) => conforms(sourceShape, v, "source"),
  source_get: (v) => conforms(sourceShape, v, "source"),
  source_patch: (v) => conforms(sourceShape, v, "source"),
  source_list: (v) => items(v, (x, w) => conforms(sourceShape, x, w), "sources"),
  source_test: (v) => conforms(testShape, v, "test"),
  source_test_failed: (v) => conforms(testShape, v, "test"),
  run_start: (v) => run(v, "run"),
  run_start_queued: (v) => run(v, "run"),
  run_cancel: (v) => run(v, "run"),
  run_running: (v) => conforms(runShape, v, "run"),
  run_detail: (v) => conforms(runShape, v, "run"),
  runs_list: (v) => items(v, run, "runs"),
  audit: (v) => items(v, (x, w) => conforms(auditShape, x, w), "audit"),
  error: (v) => conforms(errorShape, v, "error"),
};

describe("control API contract", () => {
  it("lists a check for every fixture", () => {
    const files = readdirSync(DIR).filter((f) => f.endsWith(".json")).map((f) => f.slice(0, -5)).sort();
    expect(files).toEqual(Object.keys(checks).sort());
  });
  for (const name of Object.keys(checks)) {
    it(`${name} matches control-types.ts`, () => {
      expect(checks[name](load(name))).toEqual([]);
    });
  }
  it("covers the enum values the backend returns", () => {
    expect((load("run_cancel") as Run).status).toBe("canceled");
    expect((load("run_detail") as Run).status).toBe("succeeded");
    expect((load("run_running") as Run).status).toBe("running");
    expect((load("run_start") as Run).status).toBe("queued");
  });
  it("never contains a secret value", () => {
    for (const name of Object.keys(checks)) expect(JSON.stringify(load(name))).not.toContain("SECRET");
  });
  it("detects drift (the checker is not vacuous)", () => {
    const bad = { ...(load("run_detail") as object), status: "cancelled" };
    expect(run(bad, "run")).not.toEqual([]);
    const extra = { ...(load("audit") as { items: object[] }).items[0], ts: "x" };
    expect(conforms(auditShape, extra, "a")).not.toEqual([]);
  });
});
