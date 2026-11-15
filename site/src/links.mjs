import { fileURLToPath } from "node:url";
import { posix } from "node:path";

export const repositoryRoot = fileURLToPath(new URL("../../", import.meta.url));
export const repositoryURL = "https://github.com/h0rv/pybeam";

// This is also the publication allowlist. Research and repository artifacts stay off-site.
export const pages = [
  { source: "docs/guide/index.md", path: "", label: "Overview" },
  { source: "docs/guide/quickstart.md", path: "quickstart/", label: "Quickstart" },
  { source: "docs/guide/primitives.md", path: "primitives/", label: "Core primitives" },
  { source: "docs/guide/web.md", path: "web/", label: "Web and integrations" },
  { source: "docs/guide/limits.md", path: "limits/", label: "Boundaries" },
];

export function pagePath(source) {
  return pages.find((page) => page.source === source)?.path ?? null;
}

export function withBase(path, base = "/") {
  const prefix = base.replace(/^\/+|\/+$/g, "");
  return `/${prefix ? prefix + "/" : ""}${path.replace(/^\/+/, "")}`;
}

export function rewriteLink(href, source, base = "/") {
  if (/^(?:[a-z][a-z\d+.-]*:|\/|#)/i.test(href)) return href;
  const split = href.search(/[?#]/);
  const path = split < 0 ? href : href.slice(0, split);
  const suffix = split < 0 ? "" : href.slice(split);
  if (!path) return href;
  const target = posix.normalize(posix.join(posix.dirname(source.replaceAll("\\", "/")), path));
  const page = pagePath(target);
  return page === null ? `${repositoryURL}/blob/main/${target}${suffix}` : withBase(page, base) + suffix;
}
