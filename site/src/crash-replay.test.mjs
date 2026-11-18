import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import recording from "./crash-replay.json" with { type: "json" };

const published = readFileSync(new URL("../../examples/chat_load/README.md", import.meta.url), "utf8");
const latency = ({ median, min, max }) => {
  const ms = (value) => typeof value === "number" ? value.toFixed(2) : value;
  return `${ms(median)} [${ms(min)}–${ms(max)}]`;
};

test("replay summaries match the published native-crash measurements", () => {
  assert.match(recording.source, /^https:\/\/github\.com\/h0rv\/pybeam\/blob\/[a-f0-9]{40}\/examples\/chat_load\/README\.md#measured-limits$/);
  assert.ok(published.includes("Three trials per point"));
  assert.equal(recording.trials, 3);
  const columns = ["usual", "pybeam"].map((side) =>
    `${latency(recording.baselineP99[side])} → ${latency(recording.faultP99[side])}`);
  assert.ok(published.includes(`| \`abort\` | ${columns.join(" | ")} |`));
  const { median, min, max } = recording.disconnected.usual;
  assert.ok(published.includes(`Abort disconnected ${median} [${min}–${max}] of ${recording.otherRoomSockets} other-room sockets`));
  const sends = recording.unacknowledged.usual;
  assert.ok(published.includes(`with ${sends.median} [${sends.min}–${sends.max}] versus zero scheduled sends unacknowledged`));
  for (const values of [recording.disconnected.pybeam, recording.unacknowledged.pybeam]) {
    assert.deepEqual(values, { median: 0, min: 0, max: 0 });
  }
});
