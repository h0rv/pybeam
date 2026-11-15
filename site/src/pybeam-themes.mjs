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
  text: "#211d2a", background: "#f5f3f8", keyword: "#56379b",
  string: "#14665e", builtin: "#265f85", comment: "#62596e",
  function: "#56379b", number: "#14665e", type: "#265f85",
});
export const pybeamDark = theme("pybeam-dark", "dark", {
  text: "#eee9f5", background: "#201c29", keyword: "#c7b5f5",
  string: "#85d5c9", builtin: "#a3cceb", comment: "#b5aabd",
  function: "#c7b5f5", number: "#85d5c9", type: "#a3cceb",
});
