import argparse
import asyncio
import json
import time
import traceback
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import override

import httpx
from pydantic import JsonValue, TypeAdapter
from websockets.asyncio.client import ClientConnection, connect

from benchmarks.metrics import percentile
from benchmarks.runner import measurement_lock
from benchmarks.web.run import PORT, ROOT, Variant, http_load, memory, server
from benchmarks.web.ws import ensure_client_fds

BASE = "http://127.0.0.1:" + str(PORT)
MESSAGE = TypeAdapter(tuple[str | None, str | None, str, str, dict[str, JsonValue]])


class Page(HTMLParser):
    topic: str
    session: str
    csrf: str

    def __init__(self, html: str) -> None:
        super().__init__()
        self.topic = ""
        self.session = ""
        self.csrf = ""
        self.feed(html)
        self.require_tokens()

    def require_tokens(self) -> None:
        if not self.topic or not self.csrf or not self.session:
            raise ValueError("Missing LiveView session or CSRF token")

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "csrf-token":
            self.csrf = values.get("content") or ""
        if tag == "div" and "data-phx-session" in values:
            self.session = values.get("data-phx-session") or ""
            self.topic = "lv:" + (values.get("id") or "")


@dataclass
class ViewClient:
    socket: ClientConnection
    topic: str
    ticks: int = 0
    samples: list[float] = field(default_factory=list)
    pending: dict[str, float] = field(default_factory=dict)

    async def read(self) -> None:
        async for raw in self.socket:
            _, ref, topic, event, payload = MESSAGE.validate_json(raw)
            if topic != self.topic:
                raise ValueError("Unexpected LiveView topic")
            if event == "diff":
                if not payload:
                    raise ValueError("Empty tick diff")
                self.ticks += 1
            elif event == "phx_reply" and ref in self.pending:
                if payload.get("status") != "ok":
                    raise ValueError("LiveView event failed")
                self.samples.append((time.perf_counter() - self.pending.pop(ref)) * 1000)
            else:
                raise ValueError("Unexpected LiveView message")
        raise RuntimeError("LiveView connection closed during measurement")

    async def probe(self, ref: str) -> None:
        self.pending[ref] = time.perf_counter()
        await self.socket.send(
            json.dumps(
                ["1", ref, self.topic, "event", {"type": "click", "event": "probe", "value": {}}]
            )
        )


async def open_view(client: httpx.AsyncClient, index: int) -> ViewClient:
    response = await client.get(BASE + "/")
    response.raise_for_status()
    page = Page(response.text)
    socket = await connect(
        "ws://127.0.0.1:" + str(PORT) + "/live/websocket?vsn=2.0.0",
        local_addr=("127.0.0." + str(2 + index % 4), 0),
        compression=None,
        ping_interval=None,
        max_queue=16,
        close_timeout=1,
    )
    try:
        await socket.send(
            json.dumps(
                [
                    "1",
                    "1",
                    page.topic,
                    "phx_join",
                    {
                        "url": BASE + "/",
                        "params": {"_csrf_token": page.csrf},
                        "session": page.session,
                        "static": "",
                    },
                ]
            )
        )
        async with asyncio.timeout(10):
            _, ref, topic, event, payload = MESSAGE.validate_json(await socket.recv())
        if (ref, topic, event, payload.get("status")) != ("1", page.topic, "phx_reply", "ok"):
            raise ValueError("LiveView join failed: " + str(payload))
        return ViewClient(socket, page.topic)
    except BaseException:
        await socket.close()
        raise


async def measure(name: str, count: int, seconds: int, output: Path) -> dict[str, JsonValue]:
    ensure_client_fds(count * 2)
    views: list[ViewClient] = []
    try:
        async with httpx.AsyncClient(timeout=15) as client, asyncio.TaskGroup() as group:
            readers: list[asyncio.Task[None]] = []

            async def open_tracked(index: int) -> None:
                view = await open_view(client, index)
                views.append(view)
                readers.append(group.create_task(view.read()))

            for start in range(0, count, 32):
                async with asyncio.TaskGroup() as opening:
                    for index in range(start, min(start + 32, count)):
                        opening.create_task(open_tracked(index))
            await asyncio.sleep(5)
            used = await asyncio.to_thread(memory, name)
            before = sum(view.ticks for view in views)
            started = time.monotonic()
            load = group.create_task(
                asyncio.to_thread(
                    http_load, output / (name + ".http.json"), seconds, 16, 100, "/ping"
                )
            )
            for index in range(seconds):
                for view in views[:10]:
                    await view.probe(str(index + 2))
                await asyncio.sleep(1)
            await load
            elapsed = time.monotonic() - started
            ticks = sum(view.ticks for view in views) - before
            samples = tuple(value for view in views for value in view.samples)
            pending = sum(len(view.pending) for view in views)
            for reader in readers:
                reader.cancel()
            return {
                "views": count,
                "memory_bytes": used,
                "seconds": elapsed,
                "ticks": ticks,
                "expected_ticks": count * elapsed,
                "event_samples": len(samples),
                "pending_events": pending,
                "event_p50_ms": percentile(samples, 0.5) if samples else None,
                "event_p99_ms": percentile(samples, 0.99) if samples else None,
            }
    finally:
        async with asyncio.TaskGroup() as closing:
            for view in views:
                closing.create_task(view.socket.close())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("Keep raw measurements outside the repository")
    variants: tuple[Variant, ...] = ("hornbeam", "uvicorn")
    with measurement_lock():
        output.mkdir(parents=True, exist_ok=False)
        with (output / "results.jsonl").open("w") as records:
            for repeat in range(1 if args.smoke else 3):
                for variant in variants:
                    for count in (0, 4) if args.smoke else (0, 250, 500, 1000, 2000):
                        name = "pyview-" + variant + "-" + str(repeat) + "-" + str(count)
                        record: dict[str, JsonValue] = {"variant": variant, "repeat": repeat}
                        try:
                            with server(variant, name, output, "benchmarks.web.pyview_app:app"):
                                record.update(
                                    asyncio.run(
                                        measure(name, count, 2 if args.smoke else 30, output)
                                    )
                                )
                        except Exception as error:
                            record.update({"views": count, "error": str(error)})
                            (output / (name + ".error.txt")).write_text(traceback.format_exc())
                        records.write(json.dumps(record) + "\n")
                        records.flush()
                        print(json.dumps(record), flush=True)
                        if "error" in record:
                            break


if __name__ == "__main__":
    main()
