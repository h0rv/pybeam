import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { pages, navigation, redirects, withBase } from "../src/links.mjs";

const root = fileURLToPath(new URL("../dist/", import.meta.url));
const base = (process.env.DOCS_BASE || "/").replace(/\/?$/, "/");
const origin = new URL(process.env.DOCS_SITE || "https://h0rv.github.io").origin;
let checked = 0;

function walk(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? walk(path) : [path];
  });
}

const htmlFiles = walk(root).filter((path) => path.endsWith(".html"));
assert.deepEqual(htmlFiles.map((file) => relative(root, file).replaceAll("\\", "/")).sort(),
  [...pages.map(({ path }) => `${path}index.html`), ...Object.keys(redirects).map((path) => `${path}index.html`)].sort(), "Only curated pages may be published");

for (const file of htmlFiles) {
  const page = relative(root, file).replaceAll("\\", "/");
  const html = readFileSync(file, "utf8");
  const redirect = redirects[page.replace(/index\.html$/, "")];
  if (redirect !== undefined) {
    assert.ok(html.includes(withBase(redirect, base)), `Redirect target: ${page}`);
    assert.match(html, /http-equiv="refresh"/i);
    assert.doesNotMatch(html, /<pre\b/);
  } else {
    const blocks = [...html.matchAll(/<pre\b/g)].length;
    assert.ok(blocks > 0 || page.startsWith("internals/"), `Every guide needs an executable example: ${page}`);
    for (const [block] of html.matchAll(/<pre\b[\s\S]*?<\/pre>/g)) {
      assert.match(block, /astro-code/, `Code must pass through Shiki: ${page}`);
      const colors = new Set([...block.matchAll(/(?:color|--shiki-dark):\s*(#[a-f\d]{6})/gi)].map((match) => match[1]));
      assert.ok(colors.size >= 3, `Syntax token colors must survive Markdown rendering: ${page}`);
      assert.match(block, /<span/, `Missing highlighted token spans: ${page}`);
    }
    const buttons = [...html.matchAll(/class="copy-code"/g)].length;
    assert.equal(buttons, blocks, `Code controls must be present in the initial HTML: ${page}`);
    assert.ok(html.includes('name="color-scheme"'), `Missing initial color scheme: ${page}`);
    assert.equal([...html.matchAll(/aria-current="page"/g)].length, page === "genstage/index.html" ? 0 : 1, `One current nav item: ${page}`);
    assert.match(html, /<main id="content">/, `Missing skip target: ${page}`);
    for (const { path } of navigation) {
      assert.ok(html.includes(`href="${withBase(path, base)}"`), `Missing static navigation: ${page}`);
    }
    assert.ok(!/instructor\.zig|zig-orange|build-api/.test(html), `Template branding leaked: ${page}`);
  }
  for (const match of html.matchAll(/(?:href|src)="([^"]+)"/g)) {
    const url = new URL(match[1].replaceAll("&amp;", "&"), origin + base + page);
    if (url.origin !== origin) continue;
    assert.ok(url.pathname.startsWith(base), `Link escapes deployment base: ${url}`);
    const path = join(root, decodeURIComponent(url.pathname.slice(base.length)));
    const target = url.pathname.endsWith("/") ? join(path, "index.html") : path;
    assert.ok(existsSync(target) && statSync(target).isFile(), `Missing target in ${page}: ${url}`);
    if (url.hash && target.endsWith(".html")) {
      const id = decodeURIComponent(url.hash.slice(1));
      assert.ok(readFileSync(target, "utf8").includes(`id="${id}"`), `Missing anchor in ${page}: ${url}`);
    }
    checked++;
  }
}
for (const asset of ["missing.css", "missing.LICENSE", "octicons.LICENSE", "template.LICENSE", "favicon-braces.svg"]) {
  assert.ok(existsSync(join(root, asset)), `Missing published asset: ${asset}`);
}
console.log(`Checked ${checked} local links and assets.`);
