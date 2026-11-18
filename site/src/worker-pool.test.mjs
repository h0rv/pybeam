import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { repositoryRoot } from "./links.mjs";

test("internals describe leased workers and worker-local actor loops consistently", () => {
  const markdown = ["index", "calls", "parallelism"]
    .map((name) => readFileSync(join(repositoryRoot, `docs/guide/internals/${name}.md`), "utf8"))
    .join("\n");
  assert.doesNotMatch(markdown, /Each actor owns|Actors do not run Python in parallel|every actor method body|not implemented/);
  assert.match(markdown, /Worker context[^\n]*node-wide pool/);
  assert.match(markdown, /Actor calls[^\n]*own loop/);
  assert.match(markdown, /preferred worker[^\n]*busy[^\n]*another worker/);
  assert.match(markdown, /waits up to 100 ms/);
  assert.match(markdown, /Background asyncio tasks continue between calls/);
  assert.match(markdown, /cancels and awaits its descendant tasks before releasing/);
  assert.match(markdown, /tasks on workers it previously used/);
  assert.match(markdown, /ignores cancellation keeps teardown waiting/);
  assert.match(markdown, /loop-bound objects such as Futures/);
  assert.doesNotMatch(markdown, /immediately|not guaranteed to progress/);
  for (const name of ["stack", "actor-loop"]) {
    const svg = readFileSync(join(repositoryRoot, `docs/guide/assets/${name}.svg`), "utf8");
    assert.doesNotMatch(svg, /one thread runs every actor|each with its own worker context|PyBEAM actors today|every method body/);
    assert.match(svg, /pool/i);
  }
});
