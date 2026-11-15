import assert from "node:assert/strict";
import { test } from "node:test";
import { codeToHtml } from "shiki";
import { pybeamLight, pybeamDark } from "./pybeam-themes.mjs";

function luminance(hex) {
  const channels = hex.slice(1).match(/../g).map((part) => parseInt(part, 16) / 255)
    .map((value) => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
  return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
}

for (const theme of [pybeamLight, pybeamDark]) {
  test(`${theme.name} token palette meets normal-text AA contrast`, () => {
    const background = luminance(theme.colors["editor.background"]);
    for (const foreground of [theme.colors["editor.foreground"], ...theme.tokenColors.map(({ settings }) => settings.foreground)]) {
      const text = luminance(foreground);
      const contrast = (Math.max(text, background) + .05) / (Math.min(text, background) + .05);
      assert.ok(contrast >= 4.5, `${foreground} against ${theme.colors["editor.background"]}: ${contrast}`);
    }
  });
}

test("Python keywords, strings, functions and comments have distinct highlighted tokens", async () => {
  const html = await codeToHtml('# Greet a caller\nasync def greet(name: str) -> str:\n    return "Hello " + name', {
    lang: "python", themes: { light: pybeamLight, dark: pybeamDark },
  });
  for (const color of ["#5b279e", "#00634f", "#18529a", "#505d6e"])
    assert.ok(html.toLowerCase().includes(color), color);
  assert.match(html, /--shiki-dark/);
});
