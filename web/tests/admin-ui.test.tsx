import { render, screen } from "@testing-library/react";
import { vi } from "vitest";

vi.mock("server-only", () => ({}));
vi.mock("next/link", () => ({ default: ({ href, children, ...p }: { href: string; children: React.ReactNode }) => <a href={href} {...p}>{children}</a> }));
vi.mock("next/navigation", () => ({ usePathname: () => "/admin/runs" }));
vi.mock("@/lib/control", () => ({ controlFetch: vi.fn(), controlBase: vi.fn(() => "http://control") }));

import { controlFetch, controlBase } from "@/lib/control";
import { Nav } from "@/components/Nav";
import AdminLayout from "@/app/admin/layout";
import { getMe } from "@/lib/me";
import { adminApi } from "@/lib/admin-client";

const cf = vi.mocked(controlFetch);

beforeEach(() => {
  cf.mockReset();
  vi.mocked(controlBase).mockReturnValue("http://control");
});
afterEach(() => vi.unstubAllGlobals());

describe("Nav", () => {
  it("shows Admin only with showAdmin", () => {
    const { rerender } = render(<Nav showAdmin />);
    expect(screen.getByRole("link", { name: "Admin" })).toHaveAttribute("href", "/admin");
    rerender(<Nav />);
    expect(screen.queryByRole("link", { name: "Admin" })).toBeNull();
  });
});

describe("getMe", () => {
  it("is admin for is_admin true", async () => {
    cf.mockResolvedValue({ ok: true, status: 200, data: { user: "a", is_admin: true } });
    expect(await getMe(2000)).toEqual({ state: "admin", user: "a" });
    expect(cf).toHaveBeenCalledWith("api/v1/me", expect.objectContaining({ timeoutMs: 2000 }));
  });
  it("skips the call when CONTROL_API_URL is unset", async () => {
    vi.mocked(controlBase).mockReturnValue("");
    expect((await getMe(2000)).state).toBe("unavailable");
    expect(cf).not.toHaveBeenCalled();
  });
  it("maps 403, false and errors", async () => {
    cf.mockResolvedValue({ ok: false, status: 403, error: "no" });
    expect((await getMe(2000)).state).toBe("forbidden");
    cf.mockResolvedValue({ ok: true, status: 200, data: { user: "a", is_admin: false } });
    expect((await getMe(2000)).state).toBe("forbidden");
    cf.mockResolvedValue({ ok: false, status: 502, error: "x" });
    expect((await getMe(2000)).state).toBe("unavailable");
    cf.mockRejectedValue(new Error("boom"));
    expect((await getMe(2000)).state).toBe("unavailable");
  });
});

describe("AdminLayout", () => {
  it("shows Admin only for a non-admin and renders no children", async () => {
    cf.mockResolvedValue({ ok: true, status: 200, data: { user: "a", is_admin: false } });
    render(await AdminLayout({ children: <p>SECRET CHILD</p> }));
    expect(screen.getByRole("alert")).toHaveTextContent(/Admin only/);
    expect(screen.queryByText("SECRET CHILD")).toBeNull();
    expect(cf.mock.calls.every((c) => c[0] === "api/v1/me")).toBe(true);
  });
  it("shows an unavailable message with a retry hint", async () => {
    cf.mockResolvedValue({ ok: false, status: 502, error: "x" });
    render(await AdminLayout({ children: <p>CHILD</p> }));
    expect(screen.getByRole("alert")).toHaveTextContent(/Control API unavailable/);
    expect(screen.getByRole("alert")).toHaveTextContent(/try again/i);
    expect(screen.queryByText("CHILD")).toBeNull();
  });
  it("renders children and sub-navigation for an admin", async () => {
    cf.mockResolvedValue({ ok: true, status: 200, data: { user: "a", is_admin: true } });
    render(await AdminLayout({ children: <p>CHILD</p> }));
    expect(screen.getByText("CHILD")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sources" })).toHaveAttribute("href", "/admin");
    expect(screen.getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Audit" })).not.toHaveAttribute("aria-current");
  });
});

describe("adminApi", () => {
  function stub(status: number, body: unknown) {
    const fn = vi.fn(async (_u: string, _i: RequestInit) => new Response(status === 204 ? null : JSON.stringify(body), { status }));
    vi.stubGlobal("fetch", fn);
    return fn;
  }
  it("sends the CSRF header on mutating calls only", async () => {
    const fn = stub(200, { id: "1" });
    await adminApi.get("sources");
    await adminApi.post("sources/1/run", { mode: "full" });
    await adminApi.patch("sources/1", { enabled: false });
    await adminApi.del("sources/1");
    const [get, post, patch, del] = fn.mock.calls;
    expect(get[0]).toBe("/api/admin/sources");
    expect(new Headers(get[1].headers).get("X-KG-Admin")).toBeNull();
    for (const c of [post, patch, del]) {
      expect(new Headers(c[1].headers).get("X-KG-Admin")).toBe("1");
      expect(c[1].credentials).toBe("same-origin");
    }
    expect(post[1].method).toBe("POST");
    expect(JSON.parse(String(post[1].body))).toEqual({ mode: "full" });
    expect(del[1].method).toBe("DELETE");
  });
  it("maps errors and network failures", async () => {
    stub(409, { error: "active" });
    expect(await adminApi.post("sources/1/run", {})).toEqual({ ok: false, status: 409, error: "active" });
    stub(204, null);
    expect(await adminApi.del("sources/1")).toMatchObject({ ok: true, status: 204 });
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("down"); }));
    expect(await adminApi.get("me")).toMatchObject({ ok: false, status: 502 });
  });
});
