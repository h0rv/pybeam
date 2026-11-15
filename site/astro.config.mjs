import { defineConfig } from "astro/config";
import { satteri } from "@astrojs/markdown-satteri";
import { defineHastPlugin } from "satteri";
import { fileURLToPath } from "node:url";
import { relative } from "node:path";
import { rewriteLink, repositoryRoot } from "./src/links.mjs";
import { pybeamLight, pybeamDark } from "./src/pybeam-themes.mjs";
import { codeBlocks } from "./src/code-blocks.mjs";

export default defineConfig({
  site: process.env.DOCS_SITE || "https://h0rv.github.io",
  base: process.env.DOCS_BASE || "/",
  trailingSlash: "always",
  devToolbar: { enabled: false },
  markdown: {
    shikiConfig: { themes: { light: pybeamLight, dark: pybeamDark } },
    processor: satteri({
      features: { smartPunctuation: false },
      hastPlugins: [codeBlocks, (file) => defineHastPlugin({
        name: "repository-links",
        element: {
          filter: ["a"],
          visit(node, ctx) {
            const href = node.properties.href;
            if (typeof href !== "string" || !file.fileURL) return;
            const source = relative(repositoryRoot, fileURLToPath(file.fileURL));
            ctx.setProperty(node, "href", rewriteLink(href, source, process.env.DOCS_BASE || "/"));
          },
        },
      })],
    }),
  },
});
