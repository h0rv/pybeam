const labels = ["Before the crash", "During the crash", "After the crash"];
const actions = ["Show the crash", "Show recovery", "Start again"];

for (const replay of document.querySelectorAll("[data-crash-replay]")) {
  const stages = [...replay.querySelectorAll("[data-replay-stage]")];
  const controls = replay.querySelector("[data-replay-controls]");
  const next = replay.querySelector("[data-replay-next]");
  const position = replay.querySelector("[data-replay-position]");
  let stage = 0;

  function render() {
    stages.forEach((element, index) => { element.hidden = index !== stage; });
    next.textContent = actions[stage];
    position.textContent = labels[stage];
  }

  next.addEventListener("click", () => {
    stage = (stage + 1) % stages.length;
    render();
    stages[stage].querySelector("h2").focus({ preventScroll: true });
    stages[stage].scrollIntoView({ block: "nearest" });
  });
  render();
  controls.hidden = false;
}
