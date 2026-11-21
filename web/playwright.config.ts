import { defineConfig, devices } from "@playwright/test";

// Smoke test against the mock API (e2e/mock-api.mjs); the app runs from the production build.
// Run `npm run build` first; then `npm run e2e`.
const MOCK = 8799;
const APP = 3100;

export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  retries: 0,
  // One worker: with 4 workers the desktop and phone admin specs together time out in page.goto
  // (each group passes alone; all 18 specs pass with 1 or 2 workers). Root cause not found; see the PR.
  workers: 1,
  reporter: [["list"]],
  use: { baseURL: `http://127.0.0.1:${APP}`, trace: "retain-on-failure" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
  webServer: [
    { command: `node e2e/mock-api.mjs ${MOCK}`, port: MOCK, reuseExistingServer: false },
    {
      command: `npx next start -p ${APP} -H 127.0.0.1`,
      port: APP,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { KG_API_URL: `http://127.0.0.1:${MOCK}`, CONTROL_API_URL: `http://127.0.0.1:${MOCK}`, NEXT_TELEMETRY_DISABLED: "1" },
    },
  ],
});
