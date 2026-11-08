import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import type { AuditEntry, Run, Source } from "@/lib/control-types";

vi.mock("server-only", () => ({}));
vi.mock("next/link", () => ({ default: ({ href, children, ...p }: { href: string; children: React.ReactNode }) => <a href={href} {...p}>{children}</a> }));
vi.mock("next/navigation", () => ({ usePathname: () => "/admin/runs", useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), notFound: () => { throw new Error("NOT_FOUND"); } }));
vi.mock("@/lib/control", () => ({ controlFetch: vi.fn(), controlBase: vi.fn(() => "http://control") }));
vi.mock("@/lib/admin-client", () => ({ adminApi: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() } }));

import { controlFetch } from "@/lib/control";
import { adminApi } from "@/lib/admin-client";
import RunsPage from "@/app/admin/runs/page";
import RunPage from "@/app/admin/runs/[id]/page";
import AuditPage from "@/app/admin/audit/page";
import { RunDetail } from "@/components/admin/RunDetail";
import { fmtDuration, isActive, kvText, pollDelay, parseLimit } from "@/components/admin/format";

const cf = vi.mocked(controlFetch);
const api = vi.mocked(adminApi);

const run = (o: Partial<Run> = {}): Run => ({
  id: "r1", source_id: "s1", source_name: "repo-a", kind: "git", status: "running", trigger: "manual", mode: "incremental",
  dagster_run_id: "d1", dagster_url: null, stats: null, error: null, started_at: "2026-11-08T10:00:00Z", ended_at: null, requested_by: "u",
  log_tail: [], ...o,
});

beforeEach(() => {
  cf.mockReset();
  for (const f of Object.values(api)) f.mockReset();
});
afterEach(() => vi.useRealTimers());

describe("format helpers", () => {
  it("fmtDuration", () => {
    expect(fmtDuration(null, null)).toBe("-");
    expect(fmtDuration("2026-11-08T10:00:00Z", "2026-11-08T10:00:42Z")).toBe("42s");
    expect(fmtDuration("2026-11-08T10:00:00Z", "2026-11-08T10:03:05Z")).toBe("3m 5s");
    expect(fmtDuration("2026-11-08T10:00:00Z", "2026-11-08T12:01:00Z")).toBe("2h 1m");
    expect(fmtDuration("bad", null)).toBe("-");
  });
  it("isActive", () => {
    expect(isActive("queued") && isActive("running")).toBe(true);
    expect(["succeeded", "failed", "cancelled"].some((s) => isActive(s as Run["status"]))).toBe(false);
  });
  it("kvText is compact and never HTML", () => {
    expect(kvText(null)).toBe("");
    expect(kvText({ a: 1, b: "x y", c: { d: 1 } })).toBe('a=1 b="x y" c={"d":1}');
  });
  it("pollDelay backs off after 5 errors", () => {
    expect([0, 4].map(pollDelay)).toEqual([3000, 3000]);
    expect([5, 9].map(pollDelay)).toEqual([10000, 10000]);
  });
  it("parseLimit allows only listed values", () => {
    expect(parseLimit("100")).toBe(100);
    expect(parseLimit("7")).toBe(50);
    expect(parseLimit(undefined)).toBe(50);
    expect(parseLimit(["200"])).toBe(200);
  });
});

describe("RunsPage", () => {
  const sources = { ok: true as const, status: 200, data: { items: [{ id: "s1", name: "repo-a" }] as Source[] } };
  it("renders rows, filters and passes query params", async () => {
    cf.mockImplementation(async (p: string) =>
      p.startsWith("api/v1/sources") ? sources : { ok: true, status: 200, data: { items: [run({ status: "failed" })] } });
    render(await RunsPage({ searchParams: Promise.resolve({ source_id: "s1", status: "failed", limit: "100" }) }));
    expect(cf).toHaveBeenCalledWith("api/v1/runs?source_id=s1&status=failed&limit=100");
    const row = screen.getByRole("row", { name: /repo-a/ });
    expect(within(row).getByText("failed")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: /Run r1/ })).toHaveAttribute("href", "/admin/runs/r1");
    expect(screen.getByRole("combobox", { name: "Source" })).toHaveValue("s1");
    expect(screen.getByRole("combobox", { name: "Status" })).toHaveValue("failed");
  });
  it("ignores a bad status and shows the empty state", async () => {
    cf.mockImplementation(async (p: string) => (p.startsWith("api/v1/sources") ? sources : { ok: true, status: 200, data: { items: [] } }));
    render(await RunsPage({ searchParams: Promise.resolve({ status: "<x>" }) }));
    expect(cf).toHaveBeenCalledWith("api/v1/runs?limit=50");
    expect(screen.getByText(/No runs/)).toBeInTheDocument();
  });
  it("shows an error", async () => {
    cf.mockResolvedValue({ ok: false, status: 502, error: "down" });
    render(await RunsPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("alert")).toHaveTextContent("down");
  });
});

describe("AuditPage", () => {
  it("renders compact detail as text, not HTML", async () => {
    const e: AuditEntry = { id: "a1", ts: "2026-11-08T10:00:00Z", actor: "alice", action: "source.create", target: "src_1", detail: { name: "<b>x</b>" } };
    cf.mockResolvedValue({ ok: true, status: 200, data: { items: [e] } });
    const { container } = render(await AuditPage({ searchParams: Promise.resolve({ limit: "200" }) }));
    expect(cf).toHaveBeenCalledWith("api/v1/audit?limit=200");
    expect(screen.getByText("source.create")).toBeInTheDocument();
    expect(screen.getByText("alice")).toBeInTheDocument();
    expect(screen.getByText("name=<b>x</b>")).toBeInTheDocument();
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByRole("combobox", { name: "Limit" })).toHaveValue("200");
  });
  it("shows an error", async () => {
    cf.mockResolvedValue({ ok: false, status: 500, error: "bad" });
    render(await AuditPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByRole("alert")).toHaveTextContent("bad");
  });
});

describe("RunPage", () => {
  it("calls notFound on 404", async () => {
    cf.mockResolvedValue({ ok: false, status: 404, error: "no" });
    await expect(RunPage({ params: Promise.resolve({ id: "x" }) })).rejects.toThrow("NOT_FOUND");
  });
  it("renders the detail", async () => {
    cf.mockResolvedValue({ ok: true, status: 200, data: run({ status: "succeeded" }) });
    render(await RunPage({ params: Promise.resolve({ id: "r1" }) }));
    expect(cf).toHaveBeenCalledWith("api/v1/runs/r1");
    expect(screen.getByText("succeeded")).toBeInTheDocument();
  });
});

describe("RunDetail", () => {
  const ok = (r: Run) => ({ ok: true as const, status: 200, data: r });
  const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

  it("polls every 3 s while active and stops after a final status", async () => {
    vi.useFakeTimers();
    api.get.mockResolvedValueOnce(ok(run({ status: "running" }))).mockResolvedValue(ok(run({ status: "succeeded" })));
    render(<RunDetail initial={run({ status: "queued" })} />);
    expect(api.get).not.toHaveBeenCalled();
    await tick(2999);
    expect(api.get).not.toHaveBeenCalled();
    await tick(1);
    expect(api.get).toHaveBeenCalledWith("runs/r1");
    await tick(3000);
    expect(api.get).toHaveBeenCalledTimes(2);
    expect(screen.getByText("succeeded")).toBeInTheDocument();
    await tick(30000);
    expect(api.get).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not poll a final run and shows no Cancel", () => {
    vi.useFakeTimers();
    render(<RunDetail initial={run({ status: "failed", error: "boom" })} />);
    expect(vi.getTimerCount()).toBe(0);
    expect(screen.queryByRole("button", { name: "Cancel run" })).toBeNull();
    expect(screen.getByText("boom")).toBeInTheDocument();
  });

  it("does not stack requests while one is pending", async () => {
    vi.useFakeTimers();
    api.get.mockReturnValue(new Promise(() => {}));
    render(<RunDetail initial={run()} />);
    await tick(20000);
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it("backs off to 10 s after 5 consecutive errors and shows a note", async () => {
    vi.useFakeTimers();
    api.get.mockResolvedValue({ ok: false, status: 502, error: "x" });
    render(<RunDetail initial={run()} />);
    for (let i = 0; i < 5; i++) await tick(3000);
    expect(api.get).toHaveBeenCalledTimes(5);
    expect(screen.getByText(/connection lost/i)).toBeInTheDocument();
    await tick(9999);
    expect(api.get).toHaveBeenCalledTimes(5);
    await tick(1);
    expect(api.get).toHaveBeenCalledTimes(6);
    api.get.mockResolvedValue(ok(run()));
    await tick(10000);
    expect(screen.queryByText(/connection lost/i)).toBeNull();
    await tick(3000);
    expect(api.get).toHaveBeenCalledTimes(8);
  });

  it("clears the timer on unmount", async () => {
    vi.useFakeTimers();
    api.get.mockResolvedValue(ok(run()));
    const { unmount } = render(<RunDetail initial={run()} />);
    await tick(3000);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
    await tick(10000);
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it("renders stats, escaped log tail and Dagster link", () => {
    const { container } = render(
      <RunDetail initial={run({ status: "succeeded", stats: { files: 3 }, dagster_url: "http://dag/runs/d1", log_tail: ["a <b>x</b>", "line2"] })} />,
    );
    expect(screen.getByRole("link", { name: /Dagster/ })).toHaveAttribute("href", "http://dag/runs/d1");
    expect(screen.getByText("files")).toBeInTheDocument();
    const pre = container.querySelector("pre")!;
    expect(pre).toHaveAttribute("aria-live", "off");
    expect(pre.textContent).toBe("a <b>x</b>\nline2");
    expect(pre.querySelector("b")).toBeNull();
  });

  it("cancels with confirmation and shows the cancelled state", async () => {
    const user = userEvent.setup();
    api.post.mockResolvedValue(ok(run({ status: "cancelled" })));
    render(<RunDetail initial={run()} />);
    await user.click(screen.getByRole("button", { name: "Cancel run" }));
    const dlg = screen.getByRole("alertdialog");
    expect(api.post).not.toHaveBeenCalled();
    await user.click(within(dlg).getByRole("button", { name: "Confirm cancel" }));
    expect(api.post).toHaveBeenCalledWith("runs/r1/cancel");
    expect(await screen.findByText("cancelled")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel run" })).toBeNull();
  });

  it("shows a cancel error", async () => {
    const user = userEvent.setup();
    api.post.mockResolvedValue({ ok: false, status: 502, error: "dagster down" });
    render(<RunDetail initial={run({ status: "queued" })} />);
    await user.click(screen.getByRole("button", { name: "Cancel run" }));
    await user.click(screen.getByRole("button", { name: "Confirm cancel" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("dagster down");
  });
});
