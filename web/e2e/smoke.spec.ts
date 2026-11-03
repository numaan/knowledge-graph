import { expect, test } from "@playwright/test";

// Smoke test of the main flows against the mock API. SHOTS=<dir> also saves screenshots for docs.
const shots = process.env.SHOTS;

async function shot(page: import("@playwright/test").Page, name: string, project: string) {
  if (!shots) return;
  await page.screenshot({ path: `${shots}/${project}-${name}.png`, fullPage: project === "phone" });
}

test("search, entity page, graph and feedback", async ({ page, request }, info) => {
  await page.goto("/");
  await page.getByRole("searchbox", { name: "Search the knowledge base" }).fill("invite many users");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Code" })).toBeVisible();
  await expect(page.getByRole("link", { name: "PREC-142: Bulk invite users from a CSV file" })).toBeVisible();
  await shot(page, "search", info.project.name);

  await page.getByRole("link", { name: "Tickets" }).click();
  await expect(page.getByRole("heading", { name: "Code" })).toHaveCount(0);
  await page.goBack();

  await page.getByRole("link", { name: "bulk_invite" }).first().click();
  await expect(page.getByRole("heading", { level: 1, name: "bulk_invite" })).toBeVisible();
  await expect(page.locator(".code .shiki")).toContainText("at most 500 addresses");
  await expect(page.getByText("Skip existing members in bulk invite")).toBeVisible();
  await expect(page.getByRole("group", { name: "Is this page right?" })).toBeVisible();
  await expect(page.locator(".graph canvas").first()).toBeAttached();
  await expect(page.getByRole("checkbox", { name: "CALLS" })).toBeChecked();
  await shot(page, "entity", info.project.name);

  await page.getByRole("checkbox", { name: "CALLS" }).uncheck();
  await page.getByRole("button", { name: "Outdated" }).click();
  await page.getByLabel("What is outdated? (optional)").fill("limit is now 1000");
  await page.getByRole("button", { name: "Send feedback" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Your feedback was sent" })).toBeVisible();
  const got = await (await request.get("http://127.0.0.1:8799/__feedback")).json();
  // The graph explorer expands through the proxy; ids contain ':', '/' and '#'.
  const nb = await request.get("/api/kg/node/" + encodeURIComponent("precept:backend/app/routers/users.py#bulk_invite"));
  expect(nb.status()).toBe(200);
  expect((await nb.json()).neighbors.results.length).toBe(7);
  expect((await request.get("/api/kg/../healthz")).status()).not.toBe(500);
  expect((await request.post("/api/kg/tools/x;rm", { data: {} })).status()).toBe(404);
  expect(got).toContainEqual(expect.objectContaining({ node_id: "precept:backend/app/routers/users.py#bulk_invite", kind: "outdated", comment: "limit is now 1000" }));
});

test("ask renders numbered citations that link to entity pages", async ({ page }, info) => {
  await page.goto("/ask");
  await page.getByLabel("Your question").fill("how do admins invite users in bulk?");
  await page.getByRole("button", { name: "Ask" }).click();
  const answer = page.getByRole("article", { name: /Answer to/ });
  await expect(answer).toContainText("bulk_invite");
  await expect(answer.getByRole("link", { name: /Source 1:/ }).first()).toHaveAttribute("href", /\/entity\?id=precept/);
  await expect(answer.getByRole("listitem")).toHaveCount(3);
  await shot(page, "ask", info.project.name);
  await answer.getByRole("link", { name: "UsersView.onBulkInvite", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "UsersView.onBulkInvite" })).toBeVisible();
});

test("ticket page shows the stale badge and source link; requirements and domain pages", async ({ page }, info) => {
  await page.goto("/entity?id=jira%3APREC-142");
  await expect(page.locator(".badge.warn", { hasText: "stale" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open source" })).toHaveAttribute("href", "https://example.atlassian.net/browse/PREC-142");
  await shot(page, "ticket", info.project.name);

  await page.goto("/requirements");
  await expect(page.getByRole("link", { name: "contradicted (1)" })).toBeVisible();
  await page.getByRole("link", { name: "contradicted (1)" }).click();
  await expect(page.getByText("Invitations expire after 72 hours.")).toBeVisible();
  await expect(page.getByText("An admin can invite up to 500 users")).toHaveCount(0);
  await page.goto("/requirements");
  await shot(page, "requirements", info.project.name);

  await page.goto("/domain");
  await expect(page.getByRole("heading", { name: "Capabilities" })).toBeVisible();
  await expect(page.getByText("An invitation expires after 7 days.")).toBeVisible();
  await shot(page, "domain", info.project.name);
});

test("theme toggle works from the keyboard and the page has no horizontal scroll", async ({ page }, info) => {
  await page.goto("/entity?id=" + encodeURIComponent("precept:backend/app/routers/users.py#bulk_invite"));
  const toggle = page.getByRole("button", { name: /Change theme/ });
  await toggle.focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await shot(page, "entity-dark", info.project.name);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});
