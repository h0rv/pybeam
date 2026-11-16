import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { pages, navigation, redirects, pagePath, rewriteLink, withBase, repositoryRoot } from "./links.mjs";
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
  assert.deepEqual(pages.map(({ path }) => path), ["", "actors/", "supervision/", "state/", "messaging/", "background/", "web/", "genstage/"]);
  assert.equal(new Set(pages.map(({ path }) => path)).size, pages.length);
  for (const { source, path } of pages) {
    assert.equal(pagePath(source), path);
    assert.ok(readFileSync(join(repositoryRoot, source), "utf8").startsWith("# "));
  }
  for (const source of ["README.md", "docs/research/gallery.md", "docs/guide/secret.md", "examples/actors/main.py"])
    assert.equal(pagePath(source), null);
});

test("relative guide links, anchors and queries respect deployment base", () => {
  assert.equal(rewriteLink("supervision.md#supervision", "docs/guide/index.md"), "/supervision/#supervision");
  assert.equal(rewriteLink("actors.md?view=plain#actors", "docs/guide/index.md", "/pybeam/"),
    "/pybeam/actors/?view=plain#actors");
  assert.equal(rewriteLink("index.md", "docs/guide/actors.md", "/pybeam/"), "/pybeam/");
  assert.equal(rewriteLink("../../examples/actors/main.py", "docs/guide/index.md"),
    "https://github.com/h0rv/pybeam/blob/main/examples/actors/main.py");
  for (const href of ["https://example.com/docs", "//example.com", "#memory", "mailto:help@example.com"])
    assert.equal(rewriteLink(href, "docs/guide/index.md", "/pybeam/"), href);
});

test("public guides explain API use and installation without internal process text", () => {
  const processContent = /\b(?:worktree|roadmap)\b|not yet merged|private branch|release gates/gi;
  for (const source of ["README.md", ...pages.map(({ source }) => source)]) {
    const markdown = readFileSync(join(repositoryRoot, source), "utf8");
    assert.doesNotMatch(markdown, processContent, source);
    assert.doesNotMatch(markdown, /^#{1,6} .*\bcontributors?\b/im, source);
  }
});

test("the first runnable snippet matches the ordinary async Python example", () => {
  const markdown = readFileSync(join(repositoryRoot, "docs/guide/index.md"), "utf8");
  const snippet = markdown.match(/```python\n([\s\S]*?)```/)?.[1];
  assert.equal(snippet, readFileSync(join(repositoryRoot, "examples/docs/hello.py"), "utf8"));
  assert.match(snippet, /pybeam\.run\(main\)/);
  assert.doesNotMatch(snippet, /ActorGroup|@pybeam\.actor/);
  assert.match(markdown, /\[Actors\]\(actors\.md\)/);
});

test("each concept has complete source-associated Python examples", () => {
  for (const { source, examples } of pages) {
    const markdown = readFileSync(join(repositoryRoot, source), "utf8");
    const snippets = [...markdown.matchAll(/```python\n([\s\S]*?)```/g)].map((match) => match[1]);
    assert.ok(snippets.length > 0, source);
    assert.equal(snippets.length, examples.length, source);
    snippets.forEach((snippet, index) => {
      assert.equal(snippet, readFileSync(join(repositoryRoot, examples[index]), "utf8"), source);
      assert.doesNotMatch(snippet, /\.\.\.|# (?:rest|omitted)|\b(?:Any|cast)\b|type: ignore/, source);
    });
  }
});

test("the homepage puts Python before explanations and links to the first actions", () => {
  const markdown = readFileSync(join(repositoryRoot, "docs/guide/index.md"), "utf8");
  assert.ok(markdown.slice(0, markdown.indexOf("```python")).split(/\s+/).length < 25);
  assert.equal(navigation.filter(({ label }) => label === "Get started").length, 1);
  assert.equal(redirects["quickstart/"], "");
  assert.doesNotMatch(markdown, /quickstart|Overview/);
  assert.match(markdown, /\[Actors\]\(actors\.md\)/);
  assert.doesNotMatch(markdown, /\| Capability/);
});

test("state and supervision teach distinct operations without another introduction", () => {
  const guide = (name) => readFileSync(join(repositoryRoot, `docs/guide/${name}.md`), "utf8");
  assert.match(guide("supervision"), /actors\.crash[\s\S]*actors\.rejoin/);
  assert.match(guide("supervision"), /prints `5`, then `1`/);
  assert.doesNotMatch(guide("actors"), /actors\.crash/);
  assert.match(guide("state"), /beam\.incr[\s\S]*atomic operation/);
  assert.doesNotMatch(guide("state"), /beam\.subscribe|beam\.publish/);
  for (const { source, path } of pages.filter(({ path }) => path)) {
    assert.doesNotMatch(readFileSync(join(repositoryRoot, source), "utf8"), /Hello from Python/);
  }
});

test("distinct concept tasks replace grouped capability navigation", () => {
  assert.deepEqual(navigation.map(({ label }) => label), [
    "Get started", "Actors", "Supervision", "Shared state", "Messaging", "Background services", "Web integration",
  ]);
  assert.ok(navigation.every((page) => !("group" in page)));
  assert.ok(!navigation.some(({ path }) => path === "genstage/"));
  assert.deepEqual(Object.keys(redirects), ["quickstart/", "runtime/", "subscriptions/", "limits/"]);
});
