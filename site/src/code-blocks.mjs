import { defineHastPlugin } from "satteri";

const element = (tagName, properties, children = []) => ({ type: "element", tagName, properties, children });
const text = (value) => ({ type: "text", value });

export const codeBlocks = defineHastPlugin({
  name: "static-code-controls",
  element: {
    filter: ["pre"],
    visit(node) {
      if (node.properties["data-copy-ready"]) return;
      const code = node.children.find((child) => child.type === "element" && child.tagName === "code");
      if (!code) return;
      const copy = element("pre", { ...node.properties, "data-copy-ready": true }, [...node.children]);
      const icon = element("svg", {
        "aria-hidden": "true", width: 14, height: 14, viewBox: "0 0 24 24",
        fill: "none", stroke: "currentColor", "stroke-width": 1.8,
      }, [
        element("rect", { x: 8, y: 8, width: 12, height: 12, rx: 2 }),
        element("path", { d: "M16 8V4a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4" }),
      ]);
      const button = element("button", {
        type: "button", className: ["copy-code"], "aria-label": "Copy code",
      }, [icon, element("span", {}, [text("Copy")])]);
      return element("div", { className: ["code-block"] }, [button, copy]);
    },
  },
});
