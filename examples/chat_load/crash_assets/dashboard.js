(() => {
  const live = document.getElementById("live");
  const status = document.getElementById("stream-status");
  const message = document.getElementById("stream-message");
  let lastUpdate = null;
  let focusedRun = false;

  function fresh() {
    return lastUpdate !== null && performance.now() - lastUpdate < 3000;
  }

  function controls() {
    const ready = fresh() && live.querySelector("[data-ready]")?.dataset.ready === "true";
    document.querySelectorAll("[data-run]").forEach(button => {
      button.disabled = !ready;
    });
  }

  function disconnected() {
    lastUpdate = null;
    message.textContent = "Live view interrupted. Results may be out of date.";
    status.hidden = false;
    controls();
  }

  live.addEventListener("htmx:sse:before:message", () => {
    focusedRun = document.activeElement?.id === "run-crash";
  });
  live.addEventListener("htmx:sse:after:message", () => {
    lastUpdate = performance.now();
    status.hidden = true;
    controls();
    const run = document.getElementById("run-crash");
    if (focusedRun && !run.disabled && document.activeElement === document.body) {
      run.focus({ preventScroll: true });
    }
    focusedRun = false;
  });
  live.addEventListener("htmx:sse:error", disconnected);
  live.addEventListener("htmx:sse:close", disconnected);
  document.addEventListener("click", event => {
    if (event.target.closest("[data-run]") && !fresh()) {
      event.preventDefault();
      event.stopImmediatePropagation();
      disconnected();
    }
  }, true);
  setInterval(() => {
    if (!fresh()) disconnected();
  }, 1000);
})();
