import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { pages, navigation, navigationSections, redirects, pagePath, rewriteLink, withBase, repositoryRoot } from "./links.mjs";
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
  assert.deepEqual(pages.map(({ path }) => path), ["why/", "", "actors/", "supervision/", "state/", "chat-rooms/", "messaging/", "background/", "durable-tasks/", "execution/", "failures/", "deploy/", "integrations/fastapi/", "integrations/litestar/", "integrations/pyview/", "integrations/htmx/", "internals/", "internals/measurements/", "internals/watch-for/", "genstage/"]);
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
  assert.equal(rewriteLink("why.md", "docs/guide/index.md", "/pybeam/"), "/pybeam/why/");
  assert.equal(rewriteLink("../../examples/actors/main.py", "docs/guide/index.md"),
    "https://github.com/h0rv/pybeam/blob/main/examples/actors/main.py");
  for (const href of ["https://example.com/docs", "//example.com", "#memory", "mailto:help@example.com"])
    assert.equal(rewriteLink(href, "docs/guide/index.md", "/pybeam/"), href);
});

test("guides use accessible repository-local SVGs", () => {
  const readme = readFileSync(join(repositoryRoot, "README.md"), "utf8");
  assert.match(readme, /\(docs\/guide\/why\.md\)/);
  for (const [source, prefix, name] of [
    ["why", "", "shared-state"],
    ["internals/index", "../", "stack"],
    ["internals/index", "../", "call-path"],
    ["internals/index", "../", "actor-loop"],
    ["internals/measurements", "../", "fan-out"],
  ]) {
    const guide = readFileSync(join(repositoryRoot, `docs/guide/${source}.md`), "utf8");
    assert.ok(guide.includes(`(${prefix}assets/${name}.svg)`), name);
    const svg = readFileSync(join(repositoryRoot, `docs/guide/assets/${name}.svg`), "utf8");
    assert.match(svg, /role="img" aria-labelledby="title desc"/);
    assert.match(svg, /<title id="title">[^<]+<\/title>/);
    assert.match(svg, /<desc id="desc">[^<]+<\/desc>/);
    assert.match(svg, /@media \(prefers-color-scheme: dark\)/);
    assert.match(svg, /font-family: system-ui, sans-serif/);
    assert.match(svg, /rect, path \{ fill: none;/);
    assert.doesNotMatch(svg, /<linearGradient|<radialGradient|<filter|THE STACK/);
    assert.doesNotMatch(svg, /<script|<foreignObject|(?:href|src)="https?:/i);
  }
});

test("public guides explain API use and installation without internal process text", () => {
  const processContent = /\b(?:worktree|roadmap)\b|not yet merged|private branch|release gates/gi;
  for (const source of ["README.md", ...pages.map(({ source }) => source)]) {
    const markdown = readFileSync(join(repositoryRoot, source), "utf8");
    assert.doesNotMatch(markdown, processContent, source);
    assert.doesNotMatch(markdown, /^#{1,6} .*\bcontributors?\b/im, source);
  }
});

test("user guides leave runtime internals to How PyBEAM works", () => {
  const internals = /\b(?:GIL|BEAM process(?:es)?|interpreters?|subinterpreters?|erlang-python|ErlangEventLoop|worker contexts?|Python contexts?|schedulers?)\b/;
  for (const { source } of pages.filter(({ source }) => !source.startsWith("docs/guide/internals/"))) {
    assert.doesNotMatch(readFileSync(join(repositoryRoot, source), "utf8"), internals, source);
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
    if (source === "docs/guide/integrations/htmx.md") {
      assert.match(markdown, /```html\n[\s\S]*?hx-sse:connect/);
      assert.match(markdown, /\(\.\.\/\.\.\/\.\.\/examples\/fastapi\/main\.py\)/);
    } else {
      assert.ok(snippets.length > 0 || source.startsWith("docs/guide/internals/"), source);
    }
    assert.equal(snippets.length, examples.length, source);
    snippets.forEach((snippet, index) => {
      assert.equal(snippet, readFileSync(join(repositoryRoot, examples[index]), "utf8"), source);
      for (const line of snippet.trimEnd().split("\n")) {
        assert.ok(line.length <= 79, `${source}: Python line exceeds 79 characters: ${line}`);
      }
      assert.ok(snippet.startsWith(`# ${examples[index].split("/").at(-1)}\n`), source);
      if (!["background_body.py", "durable_body.py", "genstage_transform.py"].includes(examples[index].split("/").at(-1))) {
        assert.ok(markdown.includes(`\n\n\`\`\`console\n$ uv run ${examples[index].split("/").at(-1)}\n`), source);
      }
      assert.doesNotMatch(snippet, /\.\.\.|# (?:rest|omitted)|\b(?:Any|cast)\b|type: ignore/, source);
    });
  }
});

test("the homepage puts Python before explanations and links to the first actions", () => {
  const markdown = readFileSync(join(repositoryRoot, "docs/guide/index.md"), "utf8");
  assert.ok(markdown.slice(0, markdown.indexOf("```python")).split(/\s+/).length < 45);
  assert.equal(navigation.filter(({ label }) => label === "Get started").length, 1);
  assert.equal(redirects["quickstart/"], "");
  assert.doesNotMatch(markdown, /quickstart|Overview/);
  assert.match(markdown, /\[Actors\]\(actors\.md\)/);
  assert.doesNotMatch(markdown, /\| Capability/);
});

test("state and supervision teach distinct operations without another introduction", () => {
  const guide = (name) => readFileSync(join(repositoryRoot, `docs/guide/${name}.md`), "utf8");
  assert.match(guide("supervision"), /actors\.crash[\s\S]*actors\.rejoin/);
  assert.match(guide("supervision"), /```console\n\$ uv run supervision\.py\n5\n1\n```/);
  assert.doesNotMatch(guide("actors"), /actors\.crash/);
  assert.match(guide("state"), /beam\.incr[\s\S]*atomic operation/);
  assert.doesNotMatch(guide("state"), /beam\.subscribe|beam\.publish/);
  for (const { source, path } of pages.filter(({ path }) => path)) {
    assert.doesNotMatch(readFileSync(join(repositoryRoot, source), "utf8"), /Hello from Python/);
  }
});

test("navigation groups getting started, guides, integrations and internals", () => {
  assert.deepEqual(navigation.map(({ label }) => label), [
    "Why PyBEAM", "Get started", "Actors", "Supervision", "Shared state", "Example: chat rooms", "Messaging", "Background services", "Durable tasks", "Blocking and CPU work", "Startup and failures", "Deploy and connect nodes", "FastAPI", "Litestar", "PyView", "HTMX", "Overview", "Measurements", "What to watch for",
  ]);
  assert.deepEqual(navigationSections.map(({ label }) => label), ["Get started", "Guides", "Integrations", "How PyBEAM works"]);
  assert.deepEqual(navigationSections.map(({ pages }) => pages.length), [2, 10, 4, 3]);
  assert.ok(navigationSections.at(-1).pages.every(({ source }) => source.startsWith("docs/guide/internals/")));
  assert.deepEqual(navigationSections.flatMap(({ pages }) => pages), navigation);
  assert.equal(redirects["web/"], "integrations/fastapi/");
  assert.ok(!navigation.some(({ path }) => path === "genstage/"));
  assert.deepEqual(Object.keys(redirects), ["web/", "quickstart/", "runtime/", "subscriptions/", "limits/"]);
});
