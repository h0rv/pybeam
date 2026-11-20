import assert from "node:assert/strict";
import { test } from "node:test";
import { markdownToHtml } from "satteri";
import { readFileSync } from "node:fs";
import { codeBlocks } from "./code-blocks.mjs";

test("code controls are rendered once in the HTML without changing code text", () => {
  const source = 'message = "Hi <there> & friends"';
  const { html } = markdownToHtml("\`\`\`python\n" + source + "\n\`\`\`", { hastPlugins: [codeBlocks] });
  assert.equal([...html.matchAll(/class="code-block"/g)].length, 1);
  assert.equal([...html.matchAll(/class="copy-code"/g)].length, 1);
  assert.match(html, /aria-label="Copy code"/);
  assert.match(html, /Hi &lt;there&gt; (?:&amp;|&) friends/);
  assert.doesNotMatch(html, /code-toolbar|>Python<|>Shell</);
  assert.ok(html.indexOf('class="copy-code"') < html.indexOf("<pre"));
});

test("plain code blocks retain their text without a language label", () => {
  const { html } = markdownToHtml("    plain code", { hastPlugins: [codeBlocks] });
  assert.match(html, /aria-label="Copy code"/);
  assert.match(html, /plain code/);
});

test("copy controls overlay code without reserving padding and reveal on hover or focus", () => {
  const css = readFileSync(new URL("./style.css", import.meta.url), "utf8");
  assert.match(css, /\.copy-code \{ position: absolute;/);
  assert.match(css, /\.code-block pre \{ margin: 0; border: 0; \}/);
  assert.match(css, /@media \(hover: hover\) and \(pointer: fine\) \{\s*\.copy-code \{ opacity: 0; pointer-events: none; \}/);
  assert.match(css, /\.code-block:hover \.copy-code, \.code-block:focus-within \.copy-code \{ opacity: 1; pointer-events: auto; \}/);
});
