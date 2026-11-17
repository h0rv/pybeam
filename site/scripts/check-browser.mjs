import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { spawn, execFileSync } from "node:child_process";
import { once } from "node:events";
import { fileURLToPath } from "node:url";
import { pages, navigation, withBase } from "../src/links.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = fileURLToPath(new URL("../", import.meta.url));
const base = process.env.DOCS_BASE || "/";
const port = process.env.DOCS_TEST_PORT || "43871";
assert.notEqual(port, "8642");
const origin = `http://127.0.0.1:${port}`;
const server = spawn(process.execPath, [root + "node_modules/astro/bin/astro.mjs", "preview", "--ignore-lock", "--host", "127.0.0.1", "--port", port], {
  cwd: root, env: process.env, stdio: ["ignore", "pipe", "pipe"],
});
console.log("Preview identity:", execFileSync("/bin/ps", ["-p", String(server.pid), "-o", "pid=,lstart=,command="], { encoding: "utf8" }).trim());
let output = "";
server.stdout.on("data", (data) => output += data);
server.stderr.on("data", (data) => output += data);
let browser;
let visited = 0;
let minimumContrast = Infinity;
const screenshotRoot = process.env.DOCS_SCREENSHOTS || "/tmp/pybeam-after";

try {
  const deadline = Date.now() + 10000;
  while (!output.includes(`:${port}`)) {
    assert.ok(server.exitCode === null && Date.now() < deadline, output);
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  browser = await chromium.launch({
    executablePath: process.env.CHROME_EXECUTABLE || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true, chromiumSandbox: true,
  });
  for (const colorScheme of ["light", "dark"]) {
    for (const [device, viewport] of [["desktop", { width: 1440, height: 1000 }], ["phone", { width: 390, height: 844 }]]) {
      const context = await browser.newContext({ viewport, colorScheme, permissions: ["clipboard-read", "clipboard-write"] });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("response", (response) => { if (response.status() >= 400) errors.push(response.url()); });
      for (const { path } of pages) {
        assert.equal((await page.goto(origin + base + path)).status(), 200);
        visited++;
        assert.equal(await page.locator("h1").count(), 1);
        assert.equal(await page.locator(".docs-nav a").count(), navigation.length);
        if (device === "desktop") assert.ok(await page.locator(".docs-nav a").first().isVisible(), "Desktop navigation must remain visible without opening a menu");
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Page overflow: " + path);
        const report = await page.evaluate(() => {
          const rgb = (value) => (value.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
          const background = (element) => {
            for (let node = element; node; node = node.parentElement) {
              const value = getComputedStyle(node).backgroundColor;
              if (value !== "rgba(0, 0, 0, 0)" && value !== "transparent") return value;
            }
            return "rgb(255, 255, 255)";
          };
          const luminance = (color) => {
            const values = rgb(color).map((value) => value / 255).map((value) => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
            return values[0] * .2126 + values[1] * .7152 + values[2] * .0722;
          };
          const records = [...document.querySelectorAll("article p, article li, article :not(pre) > code, .sidebar > summary, .docs-nav a, .nav-title, .page-toc a, footer a, .code-toolbar, pre code span[style]")]
            .filter((node) => node.checkVisibility() && node.textContent.trim())
            .map((node) => {
              const foreground = getComputedStyle(node).color;
              const bg = background(node);
              const a = luminance(foreground), b = luminance(bg);
              return { text: node.textContent.slice(0, 55), foreground, background: bg, ratio: (Math.max(a, b) + .05) / (Math.min(a, b) + .05) };
            });
          const colors = [...new Set([...document.querySelectorAll("pre code span[style]")].map((node) => getComputedStyle(node).color))];
          return { minimum: Math.min(...records.map(({ ratio }) => ratio)), failures: records.filter(({ ratio }) => ratio < 4.5), colors };
        });
        assert.deepEqual(report.failures, [], `${device}/${colorScheme}/${path}`);
        assert.ok(report.colors.length >= 3 || path.startsWith("internals/"), `Monochrome code: ${device}/${colorScheme}/${path}`);
        minimumContrast = Math.min(minimumContrast, report.minimum);
        if (["", "actors/", "genstage/", "web/"].includes(path)) {
          await page.screenshot({ path: `${screenshotRoot}-${path.replaceAll("/", "") || "home"}-${device}-${colorScheme}${base === "/" ? "" : "-base"}.png`, fullPage: true });
        }
        if (!path) {
          assert.ok(await page.locator("pre").first().evaluate((node) => node.getBoundingClientRect().bottom < innerHeight), "Home code must fit on the first screen");
          if (device === "desktop") assert.ok(await page.getByRole("link", { name: "Actors", exact: true }).last().evaluate((node) => node.getBoundingClientRect().top < innerHeight), "Home Actors link must be on the first screen");
        }
        if (path === "genstage/") {
          const tokenColors = await page.locator("pre code span[style]").evaluateAll((nodes) => {
            const token = (text) => nodes.find((node) => node.textContent.trim() === text);
            return ["def", "ok", "uppercase", "genstage_transform.py"].map((text) => {
              const node = token(text);
              return node ? getComputedStyle(node).color : null;
            });
          });
          assert.ok(tokenColors.every(Boolean), JSON.stringify(tokenColors));
          assert.equal(new Set(tokenColors).size, 4, "Keyword/string/function/comment must differ");
          console.log(device, colorScheme, "keyword/string/function/comment", tokenColors);
        }
      }
      await page.goto(origin + base);
      await page.keyboard.press("Tab");
      assert.equal(await page.locator(":focus").innerText(), "Skip to content");
      assert.notEqual(await page.locator(":focus").evaluate((node) => getComputedStyle(node).outlineStyle), "none");
      await page.keyboard.press("Enter");
      assert.ok(page.url().endsWith("#content"));
      if (device === "desktop") {
        await page.locator(".docs-nav").getByRole("link", { name: "Actors", exact: true }).focus();
        await page.keyboard.press("Enter");
        assert.equal(await page.locator("h1").innerText(), "Actors");
        await page.goto(origin + base);
      }
      const button = page.locator(".copy-code").first();
      await button.focus();
      await page.keyboard.press("Enter");
      await page.getByRole("status").filter({ hasText: "Code copied to clipboard." }).waitFor();
      assert.equal(await page.evaluate(() => navigator.clipboard.readText()), await page.locator("pre code").first().innerText());
      assert.deepEqual(errors, []);
      await context.close();
    }
  }
  const context = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  await page.goto(origin + base);
  await page.locator(".sidebar > summary").click();
  await page.locator(".docs-nav").getByRole("link", { name: "Actors", exact: true }).click();
  assert.equal(await page.locator("h1").innerText(), "Actors");
  assert.match(await page.locator("pre").first().innerText(), /actors\.spawn[\s\S]*counter\.add[\s\S]*counter\.read/);
  await page.locator(".sidebar > summary").click();
  await page.locator(".docs-nav").getByRole("link", { name: "Get started", exact: true }).click();
  assert.match(await page.locator('pre[data-language="python"]').innerText(), /pybeam\.run\(main\)/);
  await page.goto(origin + withBase("quickstart/", base));
  await page.waitForURL(origin + base);
  assert.equal(await page.locator("h1").innerText(), "Get started");
  assert.equal(await page.locator('.docs-nav a').filter({ hasText: /^Get started$/ }).count(), 1);
  assert.equal(await page.locator('.docs-nav a[href*="quickstart"]').count(), 0);
  await context.close();
  console.log(`Chrome passed ${visited} page/theme/viewport visits at ${base}; minimum measured text contrast ${minimumContrast.toFixed(2)}:1; keyboard, clipboard, no-JS navigation.`);
} finally {
  if (browser) await browser.close();
  server.kill("SIGTERM");
  if (server.exitCode === null) await once(server, "exit");
}
