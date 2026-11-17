import argparse
import asyncio
import json
import os
import random
import secrets
import statistics
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
from pydantic import TypeAdapter, ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from examples.chat_load.protocol import (
    Ack,
    JobAccepted,
    JobDone,
    JobRequest,
    Message,
    Push,
    RoomStats,
    Send,
)

PUSH: TypeAdapter[Push] = TypeAdapter(Push)


@dataclass(frozen=True, slots=True)
class Plan:
    url: str
    rooms: int = 4
    clients: int = 25
    seconds: float = 10.0
    rate: float = 2.0
    jobs: float = 20.0
    job_size: int = 200_000
    text_bytes: int = 64
    drain: float = 5.0


DEFAULT = Plan("")


@dataclass(frozen=True, slots=True)
class Latency:
    count: int
    p50: float
    p95: float
    p99: float
    max: float


@dataclass(frozen=True, slots=True)
class Resources:
    processes: int
    peak_rss_mb: float
    cpu_seconds: float


@dataclass(frozen=True, slots=True)
class Report:
    plan: Plan
    cpus: int
    connections: int
    connect_ms: Latency | None
    messages_sent: int
    messages_acked: int
    messages_per_second: float
    ack_ms: Latency | None
    deliveries_expected: int
    deliveries: int
    fanout_ms: Latency | None
    jobs_submitted: int
    jobs_rejected: int
    jobs_done: int
    jobs_failed: int
    job_ms: Latency | None
    counter_mismatches: int
    errors: dict[str, int]
    resources: Resources | None


def summarize(samples: Sequence[float]) -> Latency | None:
    if not samples:
        return None
    if len(samples) == 1:
        only = round(samples[0], 3)
        return Latency(1, only, only, only, only)
    cuts = statistics.quantiles(samples, n=100, method="inclusive")
    return Latency(
        len(samples),
        round(cuts[49], 3),
        round(cuts[94], 3),
        round(cuts[98], 3),
        round(max(samples), 3),
    )


def elapsed_ms(started: int) -> float:
    return (time.perf_counter_ns() - started) / 1e6


@dataclass(slots=True)
class Tally:
    sent: dict[tuple[str, str, int], int] = field(default_factory=dict)
    acked: Counter[str] = field(default_factory=Counter)
    ack_ms: list[float] = field(default_factory=list)
    fanout_ms: list[float] = field(default_factory=list)
    connect_ms: list[float] = field(default_factory=list)
    jobs: dict[str, tuple[str, int]] = field(default_factory=dict)
    done: Counter[str] = field(default_factory=Counter)
    job_ms: list[float] = field(default_factory=list)
    jobs_rejected: int = 0
    jobs_failed: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    audience: Counter[str] = field(default_factory=Counter)
    early: dict[str, tuple[int, bool]] = field(default_factory=dict)
    finished: set[str] = field(default_factory=set)
    due: int = 0
    stopped: bool = False
    settled: asyncio.Event = field(default_factory=asyncio.Event)

    def settle(self) -> None:
        if self.stopped and not self.pending:
            self.settled.set()

    def receive(self, room: str, client: str, push: Push) -> None:
        self.record(room, client, push)
        self.settle()

    def record(self, room: str, client: str, push: Push) -> None:
        match push:
            case Ack(id=id):
                started = self.sent.get((room, client, id))
                if started is None:
                    self.errors["unknown ack"] += 1
                    return
                self.acked[room] += 1
                self.ack_ms.append(elapsed_ms(started))
            case Message(sender=sender, id=id):
                started = self.sent.get((room, sender, id))
                if started is None:
                    self.errors["unknown message"] += 1
                    return
                self.due -= 1
                self.fanout_ms.append(elapsed_ms(started))
            case JobDone(job=job, result=result):
                pending = self.jobs.pop(job, None)
                if pending is None:
                    if job not in self.finished:
                        self.early.setdefault(job, (time.perf_counter_ns(), result is None))
                    return
                owner, started = pending
                self.finish(job, owner, started, time.perf_counter_ns(), failed=result is None)

    def finish(self, job: str, room: str, started: int, ended: int, *, failed: bool) -> None:
        self.finished.add(job)
        self.done[room] += 1
        self.job_ms.append((ended - started) / 1e6)
        self.jobs_failed += failed

    def accept(self, job: str, room: str, started: int) -> None:
        early = self.early.pop(job, None)
        if early is None:
            self.jobs[job] = room, started
            return
        ended, failed = early
        self.finish(job, room, started, ended, failed=failed)
        self.settle()

    def send(self, key: tuple[str, str, int]) -> None:
        self.sent[key] = time.perf_counter_ns()
        self.due += self.audience[key[0]]

    def unsend(self, key: tuple[str, str, int]) -> None:
        del self.sent[key]
        self.due -= self.audience[key[0]]

    @property
    def expected(self) -> int:
        return sum(count * self.audience[room] for room, count in self.acked.items())

    @property
    def pending(self) -> int:
        return len(self.sent) - self.acked.total() + self.due + len(self.jobs)


@dataclass(slots=True)
class Client:
    room: str
    name: str
    socket: ClientConnection
    sent: int = 0


async def listen(client: Client, tally: Tally) -> None:
    try:
        async for frame in client.socket:
            try:
                push = PUSH.validate_json(frame)
            except ValidationError:
                tally.errors["invalid push"] += 1
                continue
            tally.receive(client.room, client.name, push)
    except ConnectionClosed as error:
        if error.rcvd is None or error.rcvd.code != 1000:
            tally.errors["connection closed"] += 1


async def chat(client: Client, plan: Plan, tally: Tally, stop: float) -> None:
    text = "x" * plan.text_bytes
    while True:
        await asyncio.sleep(random.expovariate(plan.rate))
        if time.monotonic() >= stop:
            return
        key = (client.room, client.name, client.sent)
        tally.send(key)
        try:
            await client.socket.send(Send(id=client.sent, text=text).model_dump_json())
        except ConnectionClosed:
            tally.unsend(key)
            tally.errors["send failed"] += 1
            return
        client.sent += 1


async def submit(http: httpx.AsyncClient, room: str, plan: Plan, tally: Tally) -> None:
    started = time.perf_counter_ns()
    try:
        response = await http.post(
            "/jobs",
            content=JobRequest(room=room, size=plan.job_size).model_dump_json(),
            headers={"content-type": "application/json"},
        )
    except httpx.HTTPError as error:
        tally.errors[type(error).__name__] += 1
        return
    if response.status_code == 503:
        tally.jobs_rejected += 1
        return
    if response.status_code != 202:
        tally.errors["job HTTP " + str(response.status_code)] += 1
        return
    tally.accept(JobAccepted.model_validate_json(response.content).job, room, started)


async def produce_jobs(
    http: httpx.AsyncClient, rooms: Sequence[str], plan: Plan, tally: Tally, stop: float
) -> int:
    submitted = 0
    async with asyncio.TaskGroup() as tasks:
        while plan.jobs > 0:
            await asyncio.sleep(random.expovariate(plan.jobs))
            if time.monotonic() >= stop:
                break
            tasks.create_task(submit(http, rooms[submitted % len(rooms)], plan, tally))
            submitted += 1
    return submitted


async def join(plan: Plan, room: str, name: str, tally: Tally) -> Client:
    url = plan.url.replace("http", "ws", 1) + "/rooms/" + room + "/ws?client=" + name
    started = time.perf_counter_ns()
    socket = await connect(url, max_queue=None, open_timeout=30)
    tally.connect_ms.append(elapsed_ms(started))
    return Client(room, name, socket)


def tree(root: int) -> set[int]:
    found, frontier = set[int](), [root]
    while frontier:
        pid = frontier.pop()
        if pid in found:
            continue
        found.add(pid)
        try:
            for task in Path("/proc", str(pid), "task").iterdir():
                frontier.extend(int(child) for child in (task / "children").read_text().split())
        except OSError:
            continue
    return found


def usage(pids: Iterable[int]) -> tuple[int, float, float]:
    processes, rss_kb, ticks = 0, 0, 0
    for pid in {child for root in pids for child in tree(root)}:
        try:
            status = Path("/proc", str(pid), "status").read_text()
            stat = Path("/proc", str(pid), "stat").read_text()
        except OSError:
            continue
        processes += 1
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                rss_kb += int(line.split()[1])
        fields = stat.rsplit(")", 1)[1].split()
        ticks += int(fields[11]) + int(fields[12])
    return processes, rss_kb / 1024, ticks / os.sysconf("SC_CLK_TCK")


async def sample(pids: Sequence[int], done: asyncio.Event) -> Resources | None:
    if not pids:
        return None
    processes, peak, first = usage(pids)
    last = first
    while not done.is_set():
        with suppress(TimeoutError):
            async with asyncio.timeout(0.25):
                await done.wait()
        count, rss, last = usage(pids)
        processes, peak = max(processes, count), max(peak, rss)
    return Resources(processes, round(peak, 1), round(last - first, 2))


async def drain(tally: Tally, seconds: float) -> None:
    tally.stopped = True
    tally.settle()
    with suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await tally.settled.wait()


async def simulate(plan: Plan, pids: Sequence[int]) -> Report:
    tally = Tally()
    run = secrets.token_hex(3)
    rooms = ["load-" + run + "-" + str(index) for index in range(plan.rooms)]
    async with httpx.AsyncClient(base_url=plan.url, timeout=30) as http:
        (await http.get("/health")).raise_for_status()
        joined = await asyncio.gather(
            *(
                join(plan, room, "c" + str(index), tally)
                for room in rooms
                for index in range(plan.clients)
            ),
            return_exceptions=True,
        )
        clients: list[Client] = []
        for result in joined:
            match result:
                case Client():
                    clients.append(result)
                case Exception():
                    tally.errors["connect " + type(result).__name__] += 1
                case _:
                    raise result
        tally.audience.update(client.room for client in clients)
        done = asyncio.Event()
        try:
            async with asyncio.TaskGroup() as tasks:
                resources = tasks.create_task(sample(pids, done))
                for client in clients:
                    tasks.create_task(listen(client, tally))
                started = time.monotonic()
                stop = started + plan.seconds
                async with asyncio.TaskGroup() as load:
                    jobs = load.create_task(produce_jobs(http, rooms, plan, tally, stop))
                    for client in clients:
                        load.create_task(chat(client, plan, tally, stop))
                active = time.monotonic() - started
                await drain(tally, plan.drain)
                done.set()
                for client in clients:
                    await client.socket.close()
        finally:
            for client in clients:
                await client.socket.close()
        stats = [
            RoomStats.model_validate_json((await http.get("/rooms/" + room + "/stats")).content)
            for room in rooms
        ]
    mismatches = sum(
        (counted.messages != tally.acked[room]) + (counted.jobs != tally.done[room])
        for room, counted in zip(rooms, stats, strict=True)
    )
    acked = tally.acked.total()
    return Report(
        plan=plan,
        cpus=os.cpu_count() or 0,
        connections=len(clients),
        connect_ms=summarize(tally.connect_ms),
        messages_sent=len(tally.sent),
        messages_acked=acked,
        messages_per_second=round(acked / active, 1),
        ack_ms=summarize(tally.ack_ms),
        deliveries_expected=tally.expected,
        deliveries=len(tally.fanout_ms),
        fanout_ms=summarize(tally.fanout_ms),
        jobs_submitted=jobs.result(),
        jobs_rejected=tally.jobs_rejected,
        jobs_done=len(tally.job_ms),
        jobs_failed=tally.jobs_failed,
        job_ms=summarize(tally.job_ms),
        counter_mismatches=mismatches,
        errors=dict(tally.errors),
        resources=resources.result(),
    )


def latency(label: str, value: Latency | None) -> str:
    if value is None:
        return "{:<22} none".format(label)
    return "{:<22} p50 {:>8.2f}  p95 {:>8.2f}  p99 {:>8.2f}  max {:>8.2f}  (n={})".format(
        label, value.p50, value.p95, value.p99, value.max, value.count
    )


def render(report: Report) -> str:
    plan = report.plan
    lines = [
        "target                 " + plan.url,
        "load                   {} rooms x {} clients, {} msg/s each, {} jobs/s, {}s".format(
            plan.rooms, plan.clients, plan.rate, plan.jobs, plan.seconds
        ),
        "connections            {}".format(report.connections),
        latency("connect ms", report.connect_ms),
        "messages               {} sent, {} acknowledged, {}/s".format(
            report.messages_sent, report.messages_acked, report.messages_per_second
        ),
        latency("ack ms", report.ack_ms),
        "deliveries             {} of {} expected".format(
            report.deliveries, report.deliveries_expected
        ),
        latency("fan-out ms", report.fanout_ms),
        "jobs                   {} submitted, {} rejected, {} done, {} failed".format(
            report.jobs_submitted, report.jobs_rejected, report.jobs_done, report.jobs_failed
        ),
        latency("job ms", report.job_ms),
        "counter mismatches     {}".format(report.counter_mismatches),
        "errors                 {}".format(report.errors or "none"),
    ]
    if report.resources is not None:
        lines.append(
            "server processes       {}, peak RSS {} MB, CPU {} s on {} cores".format(
                report.resources.processes,
                report.resources.peak_rss_mb,
                report.resources.cpu_seconds,
                report.cpus,
            )
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate chat traffic against a chat server")
    parser.add_argument("url", help="server base URL, for example http://127.0.0.1:8800")
    parser.add_argument("--rooms", type=int, default=DEFAULT.rooms)
    parser.add_argument("--clients", type=int, default=DEFAULT.clients, help="clients per room")
    parser.add_argument("--seconds", type=float, default=DEFAULT.seconds)
    parser.add_argument("--rate", type=float, default=DEFAULT.rate, help="messages/s per client")
    parser.add_argument("--jobs", type=float, default=DEFAULT.jobs, help="background jobs/s")
    parser.add_argument("--job-size", type=int, default=DEFAULT.job_size)
    parser.add_argument("--text-bytes", type=int, default=DEFAULT.text_bytes)
    parser.add_argument("--drain", type=float, default=DEFAULT.drain)
    parser.add_argument("--pid", type=int, action="append", default=[], help="server PID to sample")
    parser.add_argument("--json", type=Path, help="also write the report as JSON")
    args = parser.parse_args()
    plan = Plan(
        args.url.rstrip("/"),
        args.rooms,
        args.clients,
        args.seconds,
        args.rate,
        args.jobs,
        args.job_size,
        args.text_bytes,
        args.drain,
    )
    report = asyncio.run(simulate(plan, args.pid))
    print(render(report))
    if args.json is not None:
        args.json.write_text(json.dumps(asdict(report), indent=2) + "\n")


if __name__ == "__main__":
    main()
