const get = (id) => document.getElementById(id);
const button = get("run");
const phases = {
  waiting: "Ready",
  baseline: "Sending HTTP requests",
  running: "The calculation is not yielding",
  found: "Runaway found",
  stopped: "Calculation stopped",
  replaced: "Corrected calculation selected",
  complete: "HTTP kept serving; counter state preserved",
  failed: "Walkthrough interrupted",
};
const calculations = {
  waiting: "The runaway job starts after two seconds of HTTP traffic.",
  baseline: "Waiting for the HTTP baseline before starting sum(-1).",
  running: "sum(-1) is running without yielding.",
  found: "The busy child's own Python frame:",
  stopped: "Child exit confirmed. The HTTP server is still running.",
  replaced: "New jobs use formula_sum; negative bounds are rejected.",
  complete: "New jobs use formula_sum; negative bounds are rejected.",
  failed: "No successful final outcome confirmed.",
};

function element(name, attributes = {}, content) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  if (content !== undefined) node.textContent = content;
  return node;
}

function chart(state) {
  const svg = get("chart");
  const width = svg.clientWidth;
  const height = width < 600 ? 230 : 285;
  svg.setAttribute("viewBox", "0 0 " + width + " " + height);
  const right = width - 12;
  const bottom = height - 40;
  const buckets = new Map();
  for (const point of state.samples) {
    const key = Math.floor(point.seconds * 4);
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(point.milliseconds);
  }
  const points = [...buckets].map(([bucket, values]) => {
    values.sort((a, b) => a - b);
    const mid = Math.floor(values.length / 2);
    return {
      bucket,
      seconds: (bucket + 0.5) / 4,
      median: values.length % 2 ? values[mid] : (values[mid - 1] + values[mid]) / 2,
      maximum: values.at(-1),
    };
  });
  const duration = Math.max(15, Math.ceil((points.at(-1)?.seconds ?? 0) / 5) * 5);
  const ceiling = Math.max(10, Math.ceil(state.max_ms / 10) * 10);
  const x = (seconds) => 44 + seconds / duration * (right - 44);
  const y = (milliseconds) => bottom - milliseconds / ceiling * (bottom - 22);
  const axes = get("axes");
  axes.replaceChildren();
  for (const value of [0, ceiling / 2, ceiling]) {
    axes.append(element("line", {x1: 44, x2: right, y1: y(value), y2: y(value), class: "grid"}));
    axes.append(element("text", {x: 34, y: y(value) + 6, "text-anchor": "end"}, value));
  }
  for (let seconds = 0; seconds <= duration; seconds += duration / 3) {
    axes.append(element("text", {x: x(seconds), y: height - 6, "text-anchor": seconds === duration ? "end" : "middle"}, seconds + "s"));
  }
  for (const key of ["median", "maximum"]) {
    let previous = -2;
    get(key).setAttribute("d", points.map((point) => {
      const command = point.bucket === previous + 1 ? "L" : "M";
      previous = point.bucket;
      return command + x(point.seconds) + "," + y(point[key]);
    }).join(" "));
  }
  const markers = get("markers");
  markers.replaceChildren();
  for (const event of state.events) {
    const marker = element("line", {x1: x(event.seconds), x2: x(event.seconds), y1: 22, y2: bottom, class: "marker"});
    marker.append(element("title", {}, event.seconds.toFixed(1) + "s: " + event.label));
    markers.append(marker);
  }
  get("chart-description").textContent = state.completed
    ? state.completed + " responses; " + state.failures + " failures. Median " + state.median_ms.toFixed(1) + "ms; maximum " + state.max_ms.toFixed(1) + "ms."
    : "No requests have been sent yet.";
}

function render(state) {
  get("status").textContent = state.error || phases[state.phase];
  get("status").classList.toggle("error", state.phase === "failed");
  get("counts").textContent = state.completed || state.failures
    ? state.completed.toLocaleString() + " responses · " + state.failures + " failed · median " + state.median_ms.toFixed(1) + " ms · max " + state.max_ms.toFixed(1) + " ms"
    : "No measurements yet.";
  get("calculation").textContent = calculations[state.phase];
  button.disabled = state.phase !== "waiting";
  button.textContent = state.phase === "waiting" ? "Run walkthrough" : state.phase === "complete" ? "Completed" : state.phase === "failed" ? "Interrupted" : "Running…";
  chart(state);
  if (!state.events.length) {
    const empty = document.createElement("li");
    empty.textContent = state.phase === "waiting" ? "Start the walkthrough to observe the calculation." : "Collecting the HTTP baseline.";
    get("events").replaceChildren(empty);
  } else {
    get("events").replaceChildren(...state.events.map((event) => {
      const item = document.createElement("li");
      const stamp = document.createElement("time");
      stamp.textContent = event.seconds.toFixed(1) + "s";
      const label = document.createElement("span");
      label.textContent = event.label;
      item.append(stamp, label);
      return item;
    }));
  }
  get("frame").hidden = !state.frame;
  if (state.frame) {
    get("location").textContent = state.frame.location;
    get("source").textContent = state.frame.source;
    get("cpu").textContent = "CPU used between samples: " + state.cpu_seconds.toFixed(2) + " s";
  }
  get("result").hidden = !state.result;
  get("result").textContent = "sum(1,000,000,000) = " + state.result;
}

async function update() {
  try {
    const response = await fetch("/state", {cache: "no-store", signal: AbortSignal.timeout(2500)});
    if (!response.ok) throw new Error("Server unavailable");
    render(await response.json());
  } catch {
    get("status").textContent = "Server disconnected; measurements stopped";
    get("status").classList.add("error");
    get("calculation").textContent = "No further outcome confirmed. Last measurements remain visible.";
    button.disabled = true;
  } finally {
    setTimeout(update, 300);
  }
}

button.addEventListener("click", async () => {
  button.disabled = true;
  try {
    const response = await fetch("/run", {method: "POST", signal: AbortSignal.timeout(2500)});
    if (!response.ok) throw new Error("Walkthrough did not start");
    render(await response.json());
  } catch {
    get("status").textContent = "Start not confirmed; checking server state";
  }
});
update();
