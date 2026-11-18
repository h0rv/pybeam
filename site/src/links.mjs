import { fileURLToPath } from "node:url";
import { posix } from "node:path";

export const repositoryRoot = fileURLToPath(new URL("../../", import.meta.url));
export const repositoryURL = "https://github.com/h0rv/pybeam";

// This is also the publication allowlist. Research and repository artifacts stay off-site.
export const pages = [
  { source: "docs/guide/why.md", path: "why/", label: "Why PyBEAM", examples: ["examples/docs/messaging.py"] },
  { source: "docs/guide/index.md", path: "", label: "Get started", examples: ["examples/docs/hello.py"] },
  { source: "docs/guide/actors.md", path: "actors/", label: "Actors", examples: ["examples/docs/actors.py"] },
  { source: "docs/guide/supervision.md", path: "supervision/", label: "Supervision", examples: ["examples/docs/supervision.py"] },
  { source: "docs/guide/state.md", path: "state/", label: "Shared state", examples: ["examples/docs/state.py"] },
  { source: "docs/guide/chat-rooms.md", path: "chat-rooms/", label: "Example: chat rooms", examples: ["examples/docs/chat_rooms.py"] },
  { source: "docs/guide/messaging.md", path: "messaging/", label: "Messaging", examples: ["examples/docs/messaging.py", "examples/docs/subscriptions.py"] },
  { source: "docs/guide/background.md", path: "background/", label: "Background services", examples: ["examples/docs/background_body.py", "examples/docs/background.py"] },
  { source: "docs/guide/durable-tasks.md", path: "durable-tasks/", label: "Durable tasks", examples: ["examples/docs/durable_tasks.py"] },
  { source: "docs/guide/execution.md", path: "execution/", label: "Blocking and CPU work", examples: ["examples/docs/execution.py"] },
  { source: "docs/guide/failures.md", path: "failures/", label: "Startup and failures", examples: ["examples/docs/reconnect.py"] },
  { source: "docs/guide/integrations/fastapi.md", path: "integrations/fastapi/", label: "FastAPI", examples: ["examples/docs/fastapi_app.py"] },
  { source: "docs/guide/integrations/litestar.md", path: "integrations/litestar/", label: "Litestar", examples: ["examples/docs/litestar_app.py"] },
  { source: "docs/guide/integrations/pyview.md", path: "integrations/pyview/", label: "PyView", examples: ["examples/docs/pyview_app.py"] },
  { source: "docs/guide/internals/index.md", path: "internals/", label: "Overview", examples: [] },
  { source: "docs/guide/internals/measurements.md", path: "internals/measurements/", label: "Measurements", examples: [] },
  { source: "docs/guide/internals/watch-for.md", path: "internals/watch-for/", label: "What to watch for", examples: [] },
  { source: "docs/guide/genstage.md", path: "genstage/", label: "Finite GenStage sample", hidden: true, examples: ["examples/docs/genstage_transform.py", "examples/docs/genstage.py"] },
];

export const navigation = pages.filter(({ hidden }) => !hidden);
export const navigationSections = [
  { label: "Get started", pages: navigation.slice(0, 2) },
  { label: "Guides", pages: navigation.slice(2, 11) },
  { label: "Integrations", pages: navigation.slice(11, 14) },
  { label: "How PyBEAM works", pages: navigation.slice(14) },
];
export const redirects = {
  "web/": "integrations/fastapi/",
  "quickstart/": "",
  "runtime/": "",
  "subscriptions/": "messaging/#notify-readers-of-a-changed-key",
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
