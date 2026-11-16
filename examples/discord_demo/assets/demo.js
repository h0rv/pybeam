htmx.config.allowEval = false;
htmx.config.allowScriptTags = false;

const connection = document.getElementById("connection");
function updateComposer() {
  const joined = document.getElementById("membership")?.dataset.joined === "true";
  document.getElementById("send-message").disabled = !joined;
}

// One observed PID per supervised room: bounded to the four rendered children.
const observedPids = new Map();
// Animate only a confirmed replacement: a PID this document already observed changed.
function observeRooms(animate) {
  for (const box of document.querySelectorAll(".children .box.child[data-room]")) {
    const room = box.dataset.room;
    const pid = box.dataset.nodePid;
    if (!room || !pid) continue;
    const previous = observedPids.get(room);
    observedPids.set(room, pid);
    if (!animate || previous === undefined || previous === pid) continue;
    box.addEventListener("animationend", () => box.classList.remove("reborn"), { once: true });
    box.classList.add("reborn");
  }
}

function afterSwap() {
  updateComposer();
  observeRooms(true);
}
updateComposer();
// First paint records the historical PIDs without animating an unobserved restart.
observeRooms(false);
// htmx registers its DOMContentLoaded setup before this deferred script.
document.addEventListener("DOMContentLoaded", () => {
  if (document.getElementById("membership")?.dataset.joined !== "false") return;
  const join = document.getElementById("join-room");
  if (!join) return;
  document.getElementById("ack").textContent = "Joining…";
  join.click();
}, { once: true });
document.addEventListener("htmx:after:swap", afterSwap);
document.addEventListener("htmx:sse:after:connection", () => {
  connection.textContent = "Connected";
  updateComposer();
});
for (const event of ["htmx:sse:error", "htmx:sse:close"]) {
  document.addEventListener(event, () => {
    connection.textContent = "Disconnected. Reconnecting…";
  });
}
document.addEventListener("htmx:error", (event) => {
  if (event.detail?.ctx?.request?.method === "POST") {
    document.getElementById("ack").textContent =
      "The action's outcome is unknown. Check the room before trying again. Nothing was retried.";
  }
});
document.addEventListener("htmx:after:request", (event) => {
  const status = event.detail?.ctx?.response?.status;
  if (status >= 400) {
    document.getElementById("ack").textContent = status < 500
      ? "Request rejected. Check your local address and reload this room. Nothing was retried."
      : "The action's outcome is unknown. Check the room before trying again.";
  }
});
