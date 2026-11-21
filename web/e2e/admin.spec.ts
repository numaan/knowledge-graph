import { expect, test, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";

// Admin flows against the mock control API (e2e/mock-control.mjs). Each test makes its own sources.
// Names carry the project name and a counter, because the desktop and phone projects share one mock.
let counter = 0;
const uniq = (info: TestInfo, label: string) => `${label}-${info.project.name}-${Date.now().toString(36)}-${++counter}`;

const headers = { "X-KG-Admin": "1", Origin: "http://127.0.0.1:3100", "Content-Type": "application/json" };

/** Seed a source through the admin proxy (the same path as the browser client). */
async function seed(request: APIRequestContext, name: string, kind: "git" | "jira" = "git", secret?: Record<string, string>) {
  const config = kind === "git" ? { url: "https://git.example.com/a.git", branch: "main" } : { base_url: "https://x.atlassian.net", projects: ["A"] };
  const res = await request.post("/api/admin/sources", { headers, data: { kind, name, config, secret } });
  expect(res.status()).toBe(201);
  return (await res.json()) as { id: string };
}

const row = (page: Page, name: string) => page.getByRole("row").filter({ has: page.getByRole("link", { name, exact: true }) });

test("lists sources with kind, last run and no secret", async ({ page, request }, info) => {
  const name = uniq(info, "list");
  const { id } = await seed(request, name, "git", { token: "tok-SECRET-123456" });
  await page.goto("/admin");
  await expect(page.getByRole("heading", { level: 1, name: "Sources" })).toBeVisible();
  const r = row(page, name);
  await expect(r).toBeVisible();
  await expect(r).toContainText("git");
  await expect(r).toContainText("never run");
  await page.goto(`/admin/sources/${id}`);
  await expect(page.getByText("Secret: set (token)")).toBeVisible();
  expect(await page.content()).not.toContain("tok-SECRET-123456");
});

test("adds a Jira source, tests it, runs it and sees the run succeed", async ({ page }, info) => {
  const name = uniq(info, "jira");
  await page.goto("/admin");
  await page.getByRole("link", { name: "Add source" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Add source" })).toBeVisible();
  await page.getByLabel("Kind").selectOption("jira");
  await page.getByLabel("Name", { exact: true }).fill(name);
  await page.getByLabel("Base URL").fill("https://example.atlassian.net");
  await page.getByLabel("Projects (keys)").fill("PREC, OPS");
  await page.getByLabel("Email", { exact: true }).fill("bot@example.com");
  await page.getByLabel("API token", { exact: true }).fill("jira-token-value");
  await page.getByRole("button", { name: "Create source" }).click();
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();

  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(page.getByRole("status").filter({ hasText: /^OK: / })).toBeVisible();

  await page.goto("/admin");
  const r = row(page, name);
  await r.getByRole("button", { name: `Run ${name}` }).click();
  await expect(r.getByRole("status")).toContainText("Run started");
  // router.refresh() re-renders the row after the click on Run; read the link after that and open it.
  const link = r.locator('a[href^="/admin/runs/"]');
  await expect(link).toBeVisible();
  await page.goto((await link.getAttribute("href"))!);
  await expect(page.getByRole("heading", { level: 1, name: /^Run run_/ })).toBeVisible();
  await expect(page.locator(".badge", { hasText: "succeeded" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("button", { name: "Cancel run" })).toHaveCount(0);
  await expect(page.getByText("code_snapshot")).toBeVisible();
});

test("edits a source and replaces the secret", async ({ page, request }, info) => {
  const name = uniq(info, "edit");
  const { id } = await seed(request, name, "git", { token: "old-token-value" });
  await page.goto(`/admin/sources/${id}`);
  await page.getByRole("button", { name: "Replace secret" }).click();
  await page.getByLabel("Token", { exact: true }).fill("new-token-value");
  await page.getByLabel("Branch").fill("develop");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Saved." })).toBeVisible();
  await expect(page.getByText("Secret: set (token)")).toBeVisible();
  const body = await page.content();
  expect(body).not.toContain("new-token-value");
  expect(body).not.toContain("old-token-value");
  const got = await (await request.get(`/api/admin/sources/${id}`)).json();
  expect(got.config.branch).toBe("develop");
  expect(JSON.stringify(got)).not.toContain("token-value");
});

test("cancels an active run", async ({ page, request }, info) => {
  const name = uniq(info, "slow");
  const { id } = await seed(request, name);
  const started = await request.post(`/api/admin/sources/${id}/run`, { headers, data: {} });
  expect(started.status()).toBe(202);
  const run = (await started.json()) as { id: string };
  await page.goto(`/admin/runs/${run.id}`);
  await expect(page.locator(".badge", { hasText: "running" })).toBeVisible();
  await page.getByRole("button", { name: "Cancel run" }).click();
  await page.getByRole("button", { name: "Confirm cancel" }).click();
  await expect(page.locator(".badge", { hasText: "canceled" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel run" })).toHaveCount(0);
  await page.goto("/admin/audit");
  await expect(page.getByRole("cell", { name: "run.cancel" }).first()).toBeVisible();
});

test("deletes a source", async ({ page, request }, info) => {
  const name = uniq(info, "del");
  const { id } = await seed(request, name);
  await page.goto(`/admin/sources/${id}`);
  await page.getByRole("button", { name: "Delete source" }).click();
  await page.getByRole("button", { name: "Confirm delete" }).click();
  await expect(page).toHaveURL(/\/admin$/);
  await expect(page.getByRole("heading", { level: 1, name: "Sources" })).toBeVisible();
  await expect(page.getByRole("link", { name, exact: true })).toHaveCount(0);
});
