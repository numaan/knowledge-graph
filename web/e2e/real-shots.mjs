// Screenshots of the web app against a real graph (docs/streams/web.md).
//   node e2e/real-shots.mjs http://127.0.0.1:3106 ../docs/streams/web-screenshots
import { chromium, devices } from "@playwright/test";

const [base = "http://127.0.0.1:3000", out = "."] = process.argv.slice(2);
const symbol = process.env.SHOT_SYMBOL ?? "precept:frontend/src/components/canvas/ScenariosDrawer.tsx#ScenariosDrawer.save";
const pages = [
  ["search", "/search?q=save+a+scenario"],
  ["entity", `/entity?id=${encodeURIComponent(symbol)}`],
  ["domain", "/domain?repo=precept"],
  ["requirements", "/requirements"],
  ["ask", "/ask"],
];

const browser = await chromium.launch();
for (const [device, opts] of [["desktop", { viewport: { width: 1280, height: 900 } }], ["phone", devices["Pixel 7"]]]) {
  const page = await browser.newPage(opts);
  for (const [name, path] of pages) {
    await page.goto(base + path, { waitUntil: "networkidle", timeout: 120_000 });
    if (name === "entity") await page.waitForTimeout(1500); // graph layout
    await page.screenshot({ path: `${out}/real-${device}-${name}.png`, fullPage: device === "phone" || name !== "entity" });
    console.log(device, name, await page.title());
  }
  await page.close();
}
await browser.close();
