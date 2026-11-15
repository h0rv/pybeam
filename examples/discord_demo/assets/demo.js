htmx.config.allowEval = false;
htmx.config.allowScriptTags = false;

const connection = document.getElementById("connection");
document.addEventListener("htmx:sse:after:connection", () => {
  connection.textContent = "Connected to native push. Fresh snapshots only.";
});
for (const event of ["htmx:sse:error", "htmx:sse:close"]) {
  document.addEventListener(event, () => {
    connection.textContent = "Push disconnected. Reconnecting with a fresh snapshot.";
  });
}
document.addEventListener("htmx:after:request", (event) => {
  if (event.detail?.ctx?.response?.status >= 400) {
    document.getElementById("ack").textContent = "Request rejected. No automatic retry. Reload if your session expired.";
  }
});
