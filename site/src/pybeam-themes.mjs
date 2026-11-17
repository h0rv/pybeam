// Shiki supplies the grammar; purple and teal distinguish Python tokens.
function theme(name, type, palette) {
  return {
    name, type,
    colors: { "editor.foreground": palette.text, "editor.background": palette.background },
    tokenColors: [
      { scope: ["keyword", "storage"], settings: { foreground: palette.keyword, fontStyle: "bold" } },
      { scope: ["keyword.operator", "punctuation"], settings: { foreground: palette.text, fontStyle: "" } },
      { scope: ["string"], settings: { foreground: palette.string } },
      { scope: ["comment"], settings: { foreground: palette.comment, fontStyle: "italic" } },
      { scope: ["entity.name.function"], settings: { foreground: palette.function, fontStyle: "bold" } },
      { scope: ["support.function.builtin"], settings: { foreground: palette.builtin, fontStyle: "" } },
      { scope: ["constant", "keyword.constant"], settings: { foreground: palette.number, fontStyle: "" } },
      { scope: ["entity.name.type", "keyword.type", "support.type"], settings: { foreground: palette.type, fontStyle: "bold" } },
    ],
  };
}

export const pybeamLight = theme("pybeam-light", "light", {
  text: "#172033", background: "#f5f7fb", keyword: "#5b279e",
  string: "#00634f", builtin: "#18529a", comment: "#505d6e",
  function: "#18529a", number: "#8a3416", type: "#704015",
});
export const pybeamDark = theme("pybeam-dark", "dark", {
  text: "#f0f3f9", background: "#1d2430", keyword: "#c9b1ff",
  string: "#83dec1", builtin: "#91c2ff", comment: "#b7c3d4",
  function: "#91c2ff", number: "#ffba98", type: "#efd18c",
});
