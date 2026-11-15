import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { pages, pagePath, rewriteLink, withBase, repositoryRoot } from "./links.mjs";
import { join } from "node:path";

test("root and project hosting paths preserve trailing slashes", () => {
  for (const base of ["/", "/pybeam/", "pybeam", "/preview/docs/"]) {
    const prefix = base === "/" ? "/" : `/${base.replace(/^\/+|\/+$/g, "")}/`;
    assert.equal(withBase("", base), prefix);
    assert.equal(withBase("quickstart/", base), `${prefix}quickstart/`);
    assert.equal(withBase("missing.css", base), `${prefix}missing.css`);
  }
});

test("only curated Markdown sources map to site pages", () => {
  assert.equal(pages.length, 5);
  assert.equal(new Set(pages.map(({ path }) => path)).size, pages.length);
  for (const { source, path } of pages) {
    assert.equal(pagePath(source), path);
    assert.ok(readFileSync(join(repositoryRoot, source), "utf8").startsWith("# "));
  }
  for (const source of ["README.md", "docs/research/gallery.md", "docs/guide/secret.md", "examples/actors/main.py"])
    assert.equal(pagePath(source), null);
});

test("relative guide links, anchors and queries respect deployment base", () => {
  assert.equal(rewriteLink("quickstart.md#run-async-python", "docs/guide/index.md"), "/quickstart/#run-async-python");
  assert.equal(rewriteLink("primitives.md?view=plain#actors", "docs/guide/quickstart.md", "/pybeam/"),
    "/pybeam/primitives/?view=plain#actors");
  assert.equal(rewriteLink("index.md", "docs/guide/primitives.md", "/pybeam/"), "/pybeam/");
  assert.equal(rewriteLink("../../examples/actors/main.py", "docs/guide/quickstart.md"),
    "https://github.com/h0rv/pybeam/blob/main/examples/actors/main.py");
  for (const href of ["https://example.com/docs", "//example.com", "#memory", "mailto:help@example.com"])
    assert.equal(rewriteLink(href, "docs/guide/index.md", "/pybeam/"), href);
});

test("public guides explain API use without contributor or installation workflows", () => {
  const processContent = /work from source|install a provided wheel|\b(?:mise|uv) (?:run|add|init|sync)\b|sibling (?:runtime |repositories|checkouts)|development_runtime|(?:platform|local) wheel|build artifacts|native (?:assets|build)|libpython|\b(?:PyPI|macOS|toolchain|worktree|roadmap)\b|\b(?:Python|CPython) \d+\.\d+|not yet merged|private branch|release gates/gi;
  for (const { source } of pages) {
    const markdown = readFileSync(join(repositoryRoot, source), "utf8");
    assert.doesNotMatch(markdown, processContent, source);
    assert.doesNotMatch(markdown, /^#{1,6} .*\b(?:install|setup|contributors?|development|build|packaging)\b/im, source);
  }
});

test("the first runnable snippet matches the ordinary async Python example", () => {
  const markdown = readFileSync(join(repositoryRoot, "docs/guide/quickstart.md"), "utf8");
  const snippet = markdown.match(/```python\n([\s\S]*?)```/)?.[1];
  assert.equal(snippet, readFileSync(join(repositoryRoot, "examples/hello.py"), "utf8"));
  assert.match(snippet, /pybeam\.run\(main\)/);
  assert.doesNotMatch(snippet, /ActorGroup|@pybeam\.actor/);
  assert.match(markdown, /\[Counter example\]\(\.\.\/\.\.\/examples\/actors\/main\.py\)/);
});

test("the core guide covers work, messages, lifetimes and web composition", () => {
  const markdown = readFileSync(join(repositoryRoot, "docs/guide/primitives.md"), "utf8");
  const headings = markdown.match(/^## .+$/gm).join("\n");
  for (const capability of [/jobs.*tasks/i, /channels/i, /message passing/i, /actors/i, /supervision.*lifetimes/i, /web/i])
    assert.match(headings, capability);
  assert.match(markdown, /ServiceSlot/);
  assert.match(markdown, /async iteration/);
  assert.match(markdown, /cleanup/);
});
