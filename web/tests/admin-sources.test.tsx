import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import type { Run, Source } from "@/lib/control-types";

const refresh = vi.fn();
const push = vi.fn();
vi.mock("server-only", () => ({}));
vi.mock("next/link", () => ({ default: ({ href, children, ...p }: { href: string; children: React.ReactNode }) => <a href={href} {...p}>{children}</a> }));
vi.mock("next/navigation", () => ({ usePathname: () => "/admin", useRouter: () => ({ refresh, push }), notFound: () => { throw new Error("NOT_FOUND"); } }));
vi.mock("@/lib/control", () => ({ controlFetch: vi.fn(), controlBase: vi.fn(() => "http://control") }));
vi.mock("@/lib/admin-client", () => ({ adminApi: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() } }));

import { controlFetch } from "@/lib/control";
import { adminApi } from "@/lib/admin-client";
import AdminPage from "@/app/admin/page";
import NewSourcePage from "@/app/admin/sources/new/page";
import EditSourcePage from "@/app/admin/sources/[id]/page";
import { SourceForm } from "@/components/admin/SourceForm";

const cf = vi.mocked(controlFetch);
const api = vi.mocked(adminApi);

const run = (o: Partial<Run> = {}): Run => ({
  id: "r1", source_id: "s1", source_name: "repo-a", kind: "git", status: "succeeded", trigger: "manual", mode: "incremental",
  dagster_run_id: null, dagster_url: null, stats: null, error: null, started_at: "2026-11-08T10:00:00Z", ended_at: null, requested_by: "u", ...o,
});
const src = (o: Partial<Source> = {}): Source => ({
  id: "s1", kind: "git", name: "repo-a", enabled: true, schedule: "0 * * * *", config: { url: "https://h/r.git", branch: "dev" },
  has_secret: false, secret_keys: [], origin: "ui", last_run: run(), created_at: "", updated_at: "", ...o,
});

beforeEach(() => {
  cf.mockReset();
  for (const f of Object.values(api)) f.mockReset();
  refresh.mockReset();
  push.mockReset();
});

describe("AdminPage", () => {
  it("renders the sources table with actions and an Add link", async () => {
    cf.mockResolvedValue({ ok: true, status: 200, data: { items: [src(), src({ id: "s2", name: "wiki", kind: "confluence", enabled: false, schedule: null, last_run: null })] } });
    render(await AdminPage());
    expect(cf).toHaveBeenCalledWith("api/v1/sources");
    const table = screen.getByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(within(rows[1]).getByText("repo-a")).toBeInTheDocument();
    expect(within(rows[1]).getByText("succeeded")).toBeInTheDocument();
    expect(within(rows[1]).getByText("0 * * * *")).toBeInTheDocument();
    expect(within(rows[1]).getByRole("link", { name: /Edit/ })).toHaveAttribute("href", "/admin/sources/s1");
    expect(within(rows[1]).getByRole("button", { name: /^Run/ })).toBeInTheDocument();
    expect(within(rows[1]).getByRole("button", { name: /^Test/ })).toBeInTheDocument();
    expect(within(rows[2]).getByText("never run")).toBeInTheDocument();
    expect(within(rows[2]).getByText("manual")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add source" })).toHaveAttribute("href", "/admin/sources/new");
  });
  it("shows an empty state", async () => {
    cf.mockResolvedValue({ ok: true, status: 200, data: { items: [] } });
    render(await AdminPage());
    expect(screen.getByText(/No sources yet/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add source" })).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });
  it("shows an error state", async () => {
    cf.mockResolvedValue({ ok: false, status: 502, error: "control API not reachable" });
    render(await AdminPage());
    expect(screen.getByRole("alert")).toHaveTextContent("control API not reachable");
  });
});

describe("row actions", () => {
  async function renderRow() {
    cf.mockResolvedValue({ ok: true, status: 200, data: { items: [src()] } });
    render(await AdminPage());
  }
  it("runs incremental directly and refreshes", async () => {
    api.post.mockResolvedValue({ ok: true, status: 202, data: run() });
    await renderRow();
    await userEvent.click(screen.getByRole("button", { name: /^Run/ }));
    expect(api.post).toHaveBeenCalledWith("sources/s1/run", { mode: "incremental" });
    await waitFor(() => expect(refresh).toHaveBeenCalled());
    expect(screen.getByRole("status")).toHaveTextContent(/started/i);
  });
  it("asks for confirmation before a full run", async () => {
    api.post.mockResolvedValue({ ok: true, status: 202, data: run() });
    await renderRow();
    await userEvent.selectOptions(screen.getByLabelText(/Mode for repo-a/), "full");
    await userEvent.click(screen.getByRole("button", { name: /^Run/ }));
    expect(api.post).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog")).toHaveTextContent(/full/i);
    await userEvent.click(screen.getByRole("button", { name: "Confirm full run" }));
    expect(api.post).toHaveBeenCalledWith("sources/s1/run", { mode: "full" });
  });
  it("cancelling the confirmation does not run", async () => {
    await renderRow();
    await userEvent.selectOptions(screen.getByLabelText(/Mode for repo-a/), "full");
    await userEvent.click(screen.getByRole("button", { name: /^Run/ }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(api.post).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });
  it("shows the run error text (409)", async () => {
    api.post.mockResolvedValue({ ok: false, status: 409, error: "a run is active" });
    await renderRow();
    await userEvent.click(screen.getByRole("button", { name: /^Run/ }));
    expect(await screen.findByText(/a run is active/)).toBeInTheDocument();
  });
  it("test shows ok or the error in a live region", async () => {
    api.post.mockResolvedValueOnce({ ok: true, status: 200, data: { ok: true, message: "reachable", details: {} } });
    await renderRow();
    await userEvent.click(screen.getByRole("button", { name: /^Test/ }));
    expect(api.post).toHaveBeenCalledWith("sources/s1/test");
    expect(await screen.findByText(/OK: reachable/)).toBeInTheDocument();
    api.post.mockResolvedValueOnce({ ok: true, status: 200, data: { ok: false, message: "auth failed", details: {} } });
    await userEvent.click(screen.getByRole("button", { name: /^Test/ }));
    expect(await screen.findByText(/Failed: auth failed/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });
  it("toggles enabled and refreshes", async () => {
    api.patch.mockResolvedValue({ ok: true, status: 200, data: src({ enabled: false }) });
    await renderRow();
    await userEvent.click(screen.getByRole("checkbox", { name: /Enabled: repo-a/ }));
    expect(api.patch).toHaveBeenCalledWith("sources/s1", { enabled: false });
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });
});

describe("SourceForm create", () => {
  it("validates the name and URL before posting", async () => {
    render(<SourceForm />);
    await userEvent.type(screen.getByLabelText("Name"), "Bad Name");
    await userEvent.click(screen.getByRole("button", { name: "Create source" }));
    expect(api.post).not.toHaveBeenCalled();
    expect(screen.getByText(/lowercase letters/)).toBeInTheDocument();
    expect(screen.getAllByText(/required/).length).toBeGreaterThan(0);
  });
  it("posts a git source with defaults and no empty secret", async () => {
    api.post.mockResolvedValue({ ok: true, status: 201, data: src({ id: "new1" }) });
    render(<SourceForm />);
    await userEvent.type(screen.getByLabelText("Name"), "my-repo");
    await userEvent.type(screen.getByLabelText("Repository URL"), "https://h/r.git");
    expect(screen.getByLabelText("Token")).toHaveAttribute("type", "password");
    await userEvent.click(screen.getByRole("button", { name: "Create source" }));
    expect(api.post).toHaveBeenCalledWith("sources", {
      kind: "git", name: "my-repo", config: { url: "https://h/r.git", branch: "main" }, schedule: null, enabled: true,
    });
    await waitFor(() => expect(push).toHaveBeenCalledWith("/admin/sources/new1"));
  });
  it("sends the secret and a preset schedule", async () => {
    api.post.mockResolvedValue({ ok: true, status: 201, data: src({ id: "n" }) });
    render(<SourceForm />);
    await userEvent.type(screen.getByLabelText("Name"), "my-repo");
    await userEvent.type(screen.getByLabelText("Repository URL"), "https://h/r.git");
    await userEvent.type(screen.getByLabelText("Token"), "tok");
    await userEvent.selectOptions(screen.getByLabelText("Schedule"), "Every 15 minutes");
    await userEvent.click(screen.getByRole("button", { name: "Create source" }));
    expect(api.post.mock.calls[0][1]).toMatchObject({ secret: { token: "tok" }, schedule: "*/15 * * * *" });
  });
  it("accepts a free cron input and rejects a bad one", async () => {
    render(<SourceForm />);
    await userEvent.type(screen.getByLabelText("Name"), "my-repo");
    await userEvent.type(screen.getByLabelText("Repository URL"), "https://h/r.git");
    await userEvent.selectOptions(screen.getByLabelText("Schedule"), "Custom cron");
    await userEvent.type(screen.getByLabelText("Cron expression (UTC)"), "* * *");
    await userEvent.click(screen.getByRole("button", { name: "Create source" }));
    expect(api.post).not.toHaveBeenCalled();
    expect(screen.getByText(/5 fields/)).toBeInTheDocument();
  });
  it("shows kind specific fields", async () => {
    render(<SourceForm />);
    await userEvent.selectOptions(screen.getByLabelText("Kind"), "jira");
    expect(screen.getByLabelText("Base URL")).toBeInTheDocument();
    expect(screen.getByLabelText(/Projects/)).toBeInTheDocument();
    expect(screen.getByLabelText("API token")).toHaveAttribute("type", "password");
    expect(screen.queryByLabelText("Repository URL")).toBeNull();
    await userEvent.selectOptions(screen.getByLabelText("Kind"), "s3");
    expect(screen.getByLabelText("Bucket")).toBeInTheDocument();
    expect(screen.getByLabelText("Secret access key")).toHaveAttribute("type", "password");
    expect(screen.queryByText(/not available yet/)).toBeNull();
    expect(screen.queryByText(/501/)).toBeNull();
    expect(screen.queryByText(/not implemented/)).toBeNull();
    expect(screen.getByText(/Without access keys the source uses anonymous access/)).toBeInTheDocument();
  });
  it("shows the API error", async () => {
    api.post.mockResolvedValue({ ok: false, status: 409, error: "name already exists" });
    render(<SourceForm />);
    await userEvent.type(screen.getByLabelText("Name"), "my-repo");
    await userEvent.type(screen.getByLabelText("Repository URL"), "https://h/r.git");
    await userEvent.click(screen.getByRole("button", { name: "Create source" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("name already exists");
  });
});

describe("SourceForm edit", () => {
  const withSecret = src({ has_secret: true, secret_keys: ["token"] });
  it("makes name and kind read-only and never prefills a secret", () => {
    render(<SourceForm source={withSecret} />);
    expect(screen.getByLabelText("Name")).toHaveAttribute("readonly");
    expect(screen.getByLabelText("Kind")).toBeDisabled();
    expect(screen.getByLabelText("Repository URL")).toHaveValue("https://h/r.git");
    expect(screen.getByText(/Secret: set/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Token")).toBeNull();
    expect(screen.getByRole("button", { name: "Replace secret" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear secret" })).toBeInTheDocument();
  });
  it("keeps the secret when Replace is not used", async () => {
    api.patch.mockResolvedValue({ ok: true, status: 200, data: withSecret });
    render(<SourceForm source={withSecret} />);
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    const body = api.patch.mock.calls[0][1] as Record<string, unknown>;
    expect(api.patch.mock.calls[0][0]).toBe("sources/s1");
    expect(body).not.toHaveProperty("secret");
    expect(body).toMatchObject({ config: { url: "https://h/r.git", branch: "dev" }, schedule: "0 * * * *", enabled: true });
    expect(body).not.toHaveProperty("name");
    expect(body).not.toHaveProperty("kind");
  });
  it("sends the new secret after Replace", async () => {
    api.patch.mockResolvedValue({ ok: true, status: 200, data: withSecret });
    render(<SourceForm source={withSecret} />);
    await userEvent.click(screen.getByRole("button", { name: "Replace secret" }));
    const field = screen.getByLabelText("Token");
    expect(field).toHaveValue("");
    await userEvent.type(field, "newtok");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(api.patch.mock.calls[0][1]).toMatchObject({ secret: { token: "newtok" } });
  });
  it("requires a value after Replace", async () => {
    render(<SourceForm source={withSecret} />);
    await userEvent.click(screen.getByRole("button", { name: "Replace secret" }));
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(api.patch).not.toHaveBeenCalled();
  });
  it("clears the secret with an empty object", async () => {
    api.patch.mockResolvedValue({ ok: true, status: 200, data: src() });
    render(<SourceForm source={withSecret} />);
    await userEvent.click(screen.getByRole("button", { name: "Clear secret" }));
    expect(screen.getByText(/will be removed/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(api.patch.mock.calls[0][1]).toMatchObject({ secret: {} });
  });
  it("shows a custom cron schedule in the free input", () => {
    render(<SourceForm source={src({ schedule: "5 4 * * 1" })} />);
    expect(screen.getByLabelText("Schedule")).toHaveValue("custom");
    expect(screen.getByLabelText("Cron expression (UTC)")).toHaveValue("5 4 * * 1");
  });
});

describe("pages", () => {
  it("new page renders the form", async () => {
    render(await NewSourcePage());
    expect(screen.getByRole("heading", { level: 1, name: "Add source" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create source" })).toBeInTheDocument();
  });
  it("edit page shows form, test, recent runs and delete", async () => {
    cf.mockImplementation(async (p: string) =>
      p === "api/v1/sources/s1"
        ? { ok: true, status: 200, data: src() }
        : { ok: true, status: 200, data: { items: [run({ id: "r9", status: "failed" })] } });
    render(await EditSourcePage({ params: Promise.resolve({ id: "s1" }) }));
    expect(cf).toHaveBeenCalledWith("api/v1/runs?source_id=s1&limit=10");
    expect(screen.getByRole("heading", { level: 1, name: "repo-a" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Test connection" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /r9|failed/ })).toHaveAttribute("href", "/admin/runs/r9");
    expect(screen.getByRole("button", { name: "Delete source" })).toBeInTheDocument();
  });
  it("edit page shows not found and errors", async () => {
    cf.mockResolvedValue({ ok: false, status: 404, error: "not found" });
    await expect(EditSourcePage({ params: Promise.resolve({ id: "x" }) })).rejects.toThrow("NOT_FOUND");
    cf.mockResolvedValue({ ok: false, status: 502, error: "down" });
    render(await EditSourcePage({ params: Promise.resolve({ id: "x" }) }));
    expect(screen.getByRole("alert")).toHaveTextContent("down");
  });
  it("delete needs confirmation and says graph data stays", async () => {
    api.del.mockResolvedValue({ ok: true, status: 204, data: null });
    cf.mockImplementation(async (p: string) =>
      p === "api/v1/sources/s1" ? { ok: true, status: 200, data: src() } : { ok: true, status: 200, data: { items: [] } });
    render(await EditSourcePage({ params: Promise.resolve({ id: "s1" }) }));
    await userEvent.click(screen.getByRole("button", { name: "Delete source" }));
    expect(api.del).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog")).toHaveTextContent(/graph data stays/i);
    await userEvent.click(screen.getByRole("button", { name: "Confirm delete" }));
    expect(api.del).toHaveBeenCalledWith("sources/s1");
    await waitFor(() => expect(push).toHaveBeenCalledWith("/admin"));
  });
  it("edit page test button shows the result", async () => {
    api.post.mockResolvedValue({ ok: true, status: 200, data: { ok: true, message: "fine", details: {} } });
    cf.mockImplementation(async (p: string) =>
      p === "api/v1/sources/s1" ? { ok: true, status: 200, data: src() } : { ok: true, status: 200, data: { items: [] } });
    render(await EditSourcePage({ params: Promise.resolve({ id: "s1" }) }));
    expect(screen.getByText(/No runs yet/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Test connection" }));
    expect(api.post).toHaveBeenCalledWith("sources/s1/test");
    expect(await screen.findByText(/OK: fine/)).toBeInTheDocument();
  });
});
