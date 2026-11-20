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
  assert.deepEqual(pages.map(({ path }) => path), ["", "get-started/", "why/", "tradeoffs/", "actors/", "supervision/", "state/", "messaging/", "background/", "durable-tasks/", "execution/", "failures/", "deploy/", "integrations/fastapi/", "integrations/litestar/", "integrations/pyview/", "integrations/htmx/", "examples/chat-rooms/", "examples/crash-containment/", "internals/", "internals/calls/", "internals/parallelism/", "internals/measurements/", "internals/watch-for/", "genstage/"]);
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
  assert.equal(rewriteLink("index.md", "docs/guide/actors.md", "/pybeam/"), "/pybeam/get-started/");
  assert.equal(rewriteLink("home.md", "docs/guide/actors.md", "/pybeam/"), "/pybeam/");
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
    ["tradeoffs", "", "tradeoffs-failures"],
    ["tradeoffs", "", "tradeoffs-state"],
    ["tradeoffs", "", "tradeoffs-recovery"],
    ["tradeoffs", "", "tradeoffs-memory"],
    ["internals/index", "../", "stack"],
    ["internals/calls", "../", "call-path"],
    ["internals/parallelism", "../", "actor-loop"],
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
    const markdown = readFileSync(join(repositoryRoot, source), "utf8");
    const content = source === "docs/guide/home.md" ? markdown.replace("[erlang-python](https://github.com/benoitc/erlang-python)", "") : markdown;
    assert.doesNotMatch(content, internals, source);
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
      assert.ok(snippets.length > 0 || source.startsWith("docs/guide/internals/") || ["docs/guide/home.md", "docs/guide/tradeoffs.md"].includes(source), source);
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

test("the homepage introduces capabilities and leaves code and measurements to linked pages", () => {
  const markdown = readFileSync(join(repositoryRoot, "docs/guide/home.md"), "utf8");
  assert.ok(markdown.split(/\s+/).length < 110);
  assert.equal(navigation.filter(({ label }) => label === "Get started").length, 1);
  for (const path of ["quickstart/", "runtime/", "limits/"]) assert.equal(redirects[path], "get-started/");
  assert.doesNotMatch(markdown, /quickstart|Overview/);
  assert.doesNotMatch(markdown, /```python|```console|\d|\bversus\b|\b(?:no|not|without)\b/i);
  assert.match(markdown, /isolated call contains a native crash/);
  assert.match(markdown, /Shared state and pub\/sub live in the runtime/);
  assert.match(markdown, /replicate through Mnesia/);
  assert.match(markdown, /https:\/\/github.com\/benoitc\/erlang-python/);
  assert.match(markdown, /https:\/\/github.com\/benoitc\/hornbeam/);
  assert.match(markdown, /Erlang\/OTP/);
  assert.match(markdown, /small memory footprint/);
  assert.match(markdown, /```sh\nuv add python-beam\n```/);
  for (const href of ["index.md", "why.md", "tradeoffs.md", "examples/crash-containment.md"]) assert.ok(markdown.includes(`](${href})`));
  assert.doesNotMatch(markdown, /\| Capability/);
});

test("crash containment compares asyncio, isolated calls and a broken process pool", () => {
  const markdown = readFileSync(join(repositoryRoot, "docs/guide/examples/crash-containment.md"), "utf8");
  assert.ok(markdown.indexOf("# crash_asyncio.py") < markdown.indexOf("# crash.py"));
  assert.match(markdown, /\$ echo \$\?\n139/);
  assert.match(markdown, /except pybeam\.TaskCrashed/);
  assert.match(markdown, /Pending call: BrokenProcessPool/);
  assert.match(markdown, /Later call: BrokenProcessPool/);
  assert.match(markdown, /external effects after a crash remain unknown/);
});

test("state and supervision teach distinct operations without another introduction", () => {
  const guide = (name) => readFileSync(join(repositoryRoot, `docs/guide/${name}.md`), "utf8");
  assert.match(guide("supervision"), /actors\.crash[\s\S]*actors\.rejoin/);
  assert.match(guide("supervision"), /```console\n\$ uv run supervision\.py\n5\n1\n```/);
  assert.doesNotMatch(guide("actors"), /actors\.crash/);
  assert.match(guide("state"), /beam\.incr[\s\S]*atomic operation/);
  assert.doesNotMatch(guide("state"), /beam\.subscribe|beam\.publish/);
  for (const { source, path } of pages.filter(({ path }) => path !== "get-started/")) {
    assert.doesNotMatch(readFileSync(join(repositoryRoot, source), "utf8"), /Hello from Python/);
  }
});

test("navigation groups getting started, guides, integrations, examples and internals", () => {
  assert.deepEqual(navigation.map(({ label }) => label), [
    "Home", "Get started", "Why PyBEAM", "Benefits and tradeoffs", "Actors", "Supervision", "Shared state", "Messaging", "Background services", "Durable tasks", "Blocking and CPU work", "Startup and failures", "Deploy and connect nodes", "FastAPI", "Litestar", "PyView", "HTMX", "Chat rooms", "Crash containment", "Overview", "Calls", "Parallel Python", "Measurements", "What to watch for",
  ]);
  assert.deepEqual(navigationSections.map(({ label }) => label), ["Get started", "Guides", "Integrations", "Examples", "How PyBEAM works"]);
  assert.deepEqual(navigationSections.map(({ pages }) => pages.length), [4, 9, 4, 2, 5]);
  assert.ok(navigationSections.at(-1).pages.every(({ source }) => source.startsWith("docs/guide/internals/")));
  assert.ok(navigationSections.at(-2).pages.every(({ source }) => source.startsWith("docs/guide/examples/")));
  assert.deepEqual(navigationSections.flatMap(({ pages }) => pages), navigation);
  assert.equal(redirects["web/"], "integrations/fastapi/");
  assert.ok(!navigation.some(({ path }) => path === "genstage/"));
  assert.deepEqual(Object.keys(redirects), ["web/", "quickstart/", "runtime/", "subscriptions/", "limits/", "chat-rooms/"]);
});
