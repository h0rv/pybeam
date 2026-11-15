# Documentation site

From the repository root:

```sh
mise run docs:setup
mise run docs:test
mise run docs:build
mise run docs:serve --port 4321
```

Open http://127.0.0.1:4321/. These tasks use Node 24.21.0 only for documentation. Node is not a core Python dependency. With Node 24 or later already available, you can also run `npm ci`, `npm test`, `npm run build`, and `npm run dev` from `site/`.

The build writes static HTML to `site/dist`. Test another hosting prefix with `DOCS_BASE=/pybeam/ mise run docs:build`. Use the same variable with `docs:serve` to preview that prefix. `DOCS_SITE` sets the site origin and defaults to `https://h0rv.github.io`.

`.github/workflows/docs-pages.yml` runs these same npm commands with Node only. Pull requests check both `/` and `/pybeam/` without accessing Pages or deploying. Main pushes and manual runs on main also build with the existing Pages origin and path, then deploy `site/dist`. A repository admin must first enable Pages with GitHub Actions as its source. The workflow does not enable Pages or change its domain. Other manual branches only run checks.

Edit the five Markdown pages in `docs/guide/`. `src/links.mjs` is the explicit publication and navigation list. No research documents or repository artifacts are published. Relative links to other repository files point to GitHub. The quickstart code is checked against `examples/actors/main.py` to keep the snippet current. Site checks do not execute native examples or establish wheel compatibility.

## Template and notices

Adapted from the clean `site/` tree of [Instructor Zig](https://horv.co/instructor.zig/) at commit `b12f0ac1fdcb4c45ef7acc614ff8aff4ab59c0e2`. That tree matched cached `origin/main` at `d971a5fad48b3e0b0b6584494cbb29d3c0250b84` when copied. Its MIT notice is preserved in `public/template.LICENSE` and included in the built site.

This retains Astro 7.4.5, @astrojs/markdown-satteri 0.4.6, Satteri 0.11.6, Shiki, and the custom layout. It does not use Starlight. The Zig API generator and deployment workflow were not copied. Navigation works without JavaScript. The only browser script is the small clipboard helper. There is no search interface in the template or this site.

missing.css 1.3.0 is bundled in `public/missing.css`. Its BSD license is in `public/missing.LICENSE`. The GitHub icon is from Octicons, with its MIT license in `public/octicons.LICENSE`.
