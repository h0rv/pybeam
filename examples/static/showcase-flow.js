(() => {
  "use strict";
  const updates = document.getElementById("updates");
  if (!updates) return;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let panel = null;
  let state = null;
  let timer = null;
  let focusedAction = null;

  function stopTimer() {
    if (timer !== null) window.clearTimeout(timer);
    timer = null;
  }

  function frames() {
    return panel ? Array.from(panel.querySelectorAll("[data-frame]")) : [];
  }

  function paint(announce = false) {
    if (!panel || !state) return;
    const recorded = Number(panel.dataset.flowCount);
    const views = frames();
    const active = panel.dataset.flowActive === "true";
    state.index = Math.max(0, Math.min(state.index, views.length - 1));
    views.forEach((view, index) => { view.hidden = index !== state.index; });
    panel.dataset.flowPlaying = String(state.mode === "playing" && !reducedMotion.matches);
    panel.querySelector("[data-flow-position]").textContent =
      "Step " + (recorded ? state.index + 1 : 0) + " of " + recorded;
    panel.querySelector("[data-flow-mode]").textContent = state.key === "ready"
      ? "Ready" : active ? "Live observations" : "Recorded replay";
    panel.querySelectorAll("[data-flow-action]").forEach((button) => {
      const action = button.dataset.flowAction;
      button.disabled = !recorded ||
        (action === "previous" && state.index === 0) ||
        (action === "next" && state.index === views.length - 1);
      if (action === "pause") {
        button.textContent = state.mode === "playing" || state.mode === "follow" ? "Pause" : "Play";
        button.setAttribute("aria-label", state.mode === "playing" || state.mode === "follow"
          ? "Pause the recorded step player" : "Play the recorded observations");
      }
    });
    if (announce && recorded) {
      panel.querySelector("[data-flow-announcement]").textContent =
        "Recorded step " + (state.index + 1) + " of " + recorded + ". " +
        views[state.index].querySelector("figcaption").textContent;
    }
    schedule();
  }

  function schedule() {
    if (!state || state.mode !== "playing" || document.hidden) {
      stopTimer();
      return;
    }
    if (timer !== null) return;
    timer = window.setTimeout(() => {
      timer = null;
      if (!panel || !state || state.mode !== "playing") return;
      const last = frames().length - 1;
      if (state.index < last) state.index += 1;
      else state.mode = "paused";
      paint();
    }, 2200);
  }

  function sync() {
    const nextPanel = document.querySelector("#current-run [data-flow-job]");
    if (!nextPanel) {
      stopTimer();
      panel = null;
      state = null;
      focusedAction = null;
      return;
    }
    const replaced = panel !== nextPanel;
    panel = nextPanel;
    const count = Number(panel.dataset.flowCount);
    if (!state || state.key !== panel.dataset.flowJob) {
      stopTimer();
      focusedAction = null;
      state = { key: panel.dataset.flowJob, index: Math.max(0, count - 1), mode: "follow", completeSeen: false };
    }
    if (state.mode === "follow") state.index = Math.max(0, count - 1);
    if (panel.dataset.flowComplete === "true" && !state.completeSeen) {
      state.completeSeen = true;
      if (state.mode === "follow" && !reducedMotion.matches && !document.hidden && count > 1) {
        state.index = 0;
        state.mode = "playing";
      } else if (state.mode === "follow") state.mode = "paused";
    }
    paint();
    // Restore only the same control after its server-rendered panel was replaced.
    // Never scroll or move focus when the user has focused something elsewhere.
    if (replaced && focusedAction && document.activeElement === document.body) {
      const control = panel.querySelector('[data-flow-action="' + focusedAction + '"]');
      if (control && !control.disabled) control.focus({ preventScroll: true });
    }
  }

  document.addEventListener("focusin", (event) => {
    const control = event.target instanceof Element ? event.target.closest("[data-flow-action]") : null;
    focusedAction = control && panel && panel.contains(control) ? control.dataset.flowAction : null;
  });

  document.addEventListener("click", (event) => {
    const button = event.target instanceof Element ? event.target.closest("[data-flow-action]") : null;
    if (!button || !panel || !state || !panel.contains(button) || button.disabled) return;
    stopTimer();
    const action = button.dataset.flowAction;
    const last = frames().length - 1;
    if (action === "previous") { state.index = Math.max(0, state.index - 1); state.mode = "paused"; }
    if (action === "next") { state.index = Math.min(last, state.index + 1); state.mode = "paused"; }
    if (action === "pause") {
      if (state.mode === "playing" || state.mode === "follow") state.mode = "paused";
      else { if (state.index === last) state.index = 0; state.mode = "playing"; }
    }
    if (action === "replay") { state.index = 0; state.mode = "playing"; }
    if (action === "latest") { state.index = last; state.mode = panel.dataset.flowActive === "true" ? "follow" : "paused"; }
    paint(true);
  });

  function pausePresentation() {
    if (state && (state.mode === "playing" || state.mode === "follow")) {
      state.mode = "paused";
      paint();
    }
  }
  document.addEventListener("visibilitychange", () => { if (document.hidden) pausePresentation(); });
  reducedMotion.addEventListener("change", () => { if (reducedMotion.matches) pausePresentation(); });
  // HTMX may settle server attributes after insertion. Reapply presentation state
  // after that step as well as on direct authoritative panel replacements.
  document.addEventListener("htmx:after:swap", sync);
  document.addEventListener("htmx:after:settle", sync);
  // Only watch replacement of #jobs, not our own text and visibility changes.
  new MutationObserver(sync).observe(updates, { childList: true });
  sync();
})();
