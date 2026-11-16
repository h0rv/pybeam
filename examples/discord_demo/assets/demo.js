htmx.config.allowEval = false;
htmx.config.allowScriptTags = false;

const connection = document.getElementById("connection");
function updateComposer() {
  const joined = document.getElementById("membership")?.dataset.joined === "true";
  document.getElementById("send-message").disabled = !joined;
}
updateComposer();
document.addEventListener("htmx:after:swap", updateComposer);
document.addEventListener("htmx:sse:after:connection", () => {
  connection.textContent = "Connected";
  updateComposer();
});
for (const event of ["htmx:sse:error", "htmx:sse:close"]) {
  document.addEventListener(event, () => {
    connection.textContent = "Disconnected. Reconnecting…";
  });
}
document.addEventListener("htmx:after:request", (event) => {
  const status = event.detail?.ctx?.response?.status;
  if (status >= 400) {
    document.getElementById("ack").textContent = status < 500
      ? "Request rejected. Check your local address and reload this room. Nothing was retried."
      : "The action's outcome is unknown. Check the room before trying again.";
  }
});
