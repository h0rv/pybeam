import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { repositoryRoot } from "./links.mjs";

test("development checkout follows the runtime entry in the lock", () => {
  const markdown = readFileSync(join(repositoryRoot, "docs/development.md"), "utf8");
  const assignment = markdown.match(/^runtime_ref=\$\(.+\)$/m)?.[0];
  assert.ok(assignment, "read the runtime revision from rebar.lock");
  assert.match(markdown, /^git -C \.\.\/erlang-python checkout "\$runtime_ref"$/m);
  const directory = mkdtempSync(join(tmpdir(), "pybeam-doc-pin-"));
  try {
    for (const revision of ["a".repeat(40), "b".repeat(40)]) {
      writeFileSync(join(directory, "rebar.lock"),
        `[{<<"other">>,{git,"other",{ref,"${"c".repeat(40)}"}},0},\n` +
        `{<<"erlang_python">>,\n{git,"runtime",\n{ref,"${revision}"}},0}].\n`);
      assert.equal(execFileSync("sh", ["-ec", `${assignment}\nprintf '%s' "$runtime_ref"`],
        { cwd: directory, encoding: "utf8" }), revision);
    }
  } finally {
    rmSync(directory, { recursive: true });
  }
});
