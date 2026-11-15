import { fileURLToPath } from "node:url";
import { posix } from "node:path";

export const repositoryRoot = fileURLToPath(new URL("../../", import.meta.url));
export const repositoryURL = "https://github.com/h0rv/pybeam";

// This is also the publication allowlist. Research and repository artifacts stay off-site.
export const pages = [
  { source: "docs/guide/index.md", path: "", label: "Get started", examples: ["examples/docs/hello.py"] },
  { source: "docs/guide/actors.md", path: "actors/", label: "Actors", examples: ["examples/docs/actors.py", "examples/docs/supervision.py"] },
  { source: "docs/guide/messaging.md", path: "messaging/", label: "Shared state and messages", examples: ["examples/docs/state.py", "examples/docs/messaging.py", "examples/docs/subscriptions.py"] },
  { source: "docs/guide/background.md", path: "background/", label: "Background services", examples: ["examples/docs/background_body.py", "examples/docs/background.py"] },
  { source: "docs/guide/web.md", path: "web/", label: "Web integration", examples: ["examples/docs/web.py"] },
  { source: "docs/guide/genstage.md", path: "genstage/", label: "Finite GenStage sample", hidden: true, examples: ["examples/docs/genstage_transform.py", "examples/docs/genstage.py"] },
];

export const navigation = pages.filter(({ hidden }) => !hidden);
export const redirects = {
  "quickstart/": "",
  "runtime/": "",
  "state/": "messaging/#store-a-value",
  "subscriptions/": "messaging/#notify-readers-of-a-changed-key",
  "supervision/": "actors/#after-a-failure",
  "limits/": "",
};

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
