import argparse
import asyncio
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import field
from time import perf_counter
from uuid import uuid4

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict

import pybeam
from pybeam.integrations.hornbeam import Beam, run

COMMITTED = "customer=42"
UNCOMMITTED = "draft=unconfirmed"
HANDOFF = "billing"
TOOL_DELAY = 3.0


class Turn(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: list[str]
    route: str = ""


async def choose_route(state: Turn) -> dict[str, str]:
    route = HANDOFF if state.messages[-1] == HANDOFF else "local"
    model = FakeListChatModel(responses=[route])
    decision = await model.ainvoke([HumanMessage(text) for text in state.messages])
    return {"route": decision.text}


def new_graph() -> StateGraph[Turn]:
    graph = StateGraph(Turn)
    graph.add_node("choose_route", choose_route)
    graph.add_edge(START, "choose_route")
    graph.add_edge("choose_route", END)
    return graph


GRAPH = new_graph().compile()


async def run_turn(history: list[str], prompt: str) -> Turn:
    result = await GRAPH.ainvoke(Turn(messages=[*history, prompt]))
    return Turn.model_validate(result)


async def stalled_tool(signal_started: Callable[[], Awaitable[None]]) -> None:
    await signal_started()
    await asyncio.sleep(TOOL_DELAY)


@pybeam.actor
class Session:
    history: list[str] = field(default_factory=list)

    async def turn(self, prompt: str, *, save: bool = False) -> str:
        result = await run_turn(self.history, prompt)
        self.history = result.messages
        if save:
            await pybeam.write_state(self)
        return result.route

    async def transcript(self) -> list[str]:
        return self.history

    async def stall(self, topic: str) -> None:
        beam = await Beam.connect()

        async def signal_started() -> None:
            await beam.publish(topic, b"tool-started")

        await stalled_tool(signal_started)

    async def handoff(self, topic: str) -> None:
        result = await run_turn(self.history, HANDOFF)
        self.history = result.messages
        beam = await Beam.connect()
        await beam.publish(topic, result.route.encode())


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def probe_latency(session: Session) -> float:
    start = perf_counter()
    await session.transcript()
    return (perf_counter() - start) * 1000


async def beam_main() -> None:
    args = arguments()
    beam = await Beam.connect()
    key = "agent-probe-" + uuid4().hex
    start_topic = key + ":started"
    handoff_topic = key + ":handoff"
    async with (
        asyncio.timeout(45),
        pybeam.ActorGroup(max_children=args.sessions) as group,
        pybeam.ActorGroup(max_children=1) as control_group,
        beam.subscribe(start_topic) as starts,
        beam.subscribe(handoff_topic) as handoffs,
    ):
        control = await control_group.spawn(Session)
        start = perf_counter()
        victim = await group.restore(key, Session)
        peers = [await group.spawn(Session) for _ in range(args.sessions - 1)]
        spawn_ms = (perf_counter() - start) * 1000
        await victim.turn(COMMITTED, save=True)
        await victim.turn(UNCOMMITTED)
        before = await group.info(victim)
        specialist_before = await group.info(peers[0])
        async with asyncio.TaskGroup() as tasks:
            fault = tasks.create_task(victim.stall(start_topic))
            require(await starts.receive() == b"tool-started", "Wrong tool-start event")
            start = perf_counter()
            turns = [tasks.create_task(peer.turn("heartbeat")) for peer in peers]
            routes = await asyncio.gather(*turns)
            require(routes == ["local"] * len(peers), "A healthy turn did not return a route")
            healthy_ms = (perf_counter() - start) * 1000
            await peers[-1].handoff(handoff_topic)
            route = (await handoffs.receive()).decode()
            require(route == HANDOFF, "The model did not route to billing")
            await peers[0].turn(route)
            require(not fault.done(), "The healthy turns did not overlap the stalled tool")
            start = perf_counter()
            fault.cancel()
            with suppress(asyncio.CancelledError):
                await fault
            require((await group.info(victim)).admitted_count == 1, "The tool is not in flight")
            crash = tasks.create_task(group.crash(victim))
            await asyncio.sleep(0.05)
            require(not crash.done(), "There was no diagnostic drain to measure")
            same_group = tasks.create_task(probe_latency(peers[0]))
            other_group = tasks.create_task(probe_latency(control))
            await crash
            replacement = await group.rejoin(victim)
            recovered = await replacement.transcript()
            recovery_ms = (perf_counter() - start) * 1000
        after = await group.info(replacement)
        specialist_after = await group.info(peers[0])
        require(before.generation != after.generation, "The stopped process was not replaced")
        require(recovered == [COMMITTED], "The replacement loaded the wrong snapshot")
        require(
            specialist_before.pid == specialist_after.pid
            and specialist_before.generation == specialist_after.generation
            and await peers[0].transcript() == ["heartbeat", route],
            "The billing specialist lost its generation or in-memory state",
        )
        print(
            "pybeam sessions=%d spawn_ms=%.1f healthy=%d healthy_ms=%.1f handoff=%s"
            " fault=diagnostic-kill generation=%d->%d recovery_ms=%.1f"
            " drain_probe_same_group_ms=%.1f drain_probe_other_group_ms=%.1f transcript=%s"
            % (
                args.sessions,
                spawn_ms,
                len(peers),
                healthy_ms,
                route,
                before.generation,
                after.generation,
                recovery_ms,
                same_group.result(),
                other_group.result(),
                recovered,
            ),
            flush=True,
        )


async def plain_main() -> None:
    args = arguments()
    ready = asyncio.Event()
    handoffs: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    seed = await run_turn([], COMMITTED)
    snapshot = seed.messages.copy()
    draft = await run_turn(seed.messages, UNCOMMITTED)

    async def signal_started() -> None:
        ready.set()

    async with asyncio.timeout(45), asyncio.TaskGroup() as tasks:
        fault = tasks.create_task(stalled_tool(signal_started))
        await ready.wait()
        start = perf_counter()
        turns = [tasks.create_task(run_turn([], "heartbeat")) for _ in range(args.sessions - 1)]
        peers = await asyncio.gather(*turns)
        healthy_ms = (perf_counter() - start) * 1000
        await handoffs.put((await run_turn(peers[-1].messages, HANDOFF)).route)
        route = await handoffs.get()
        specialist = await run_turn(peers[0].messages, route)
        require(
            specialist.messages == ["heartbeat", HANDOFF] and not fault.done(),
            "The handoff did not finish during the fault",
        )
        start = perf_counter()
        fault.cancel()
        with suppress(asyncio.CancelledError):
            await fault
        fresh: list[str] = []
        recovered = snapshot.copy()
        recovery_ms = (perf_counter() - start) * 1000
    require(
        fault.cancelled()
        and draft.messages == [COMMITTED, UNCOMMITTED]
        and recovered == [COMMITTED],
        "The baseline did not cancel the tool and reload its application-owned checkpoint",
    )
    print(
        "asyncio sessions=%d healthy=%d healthy_ms=%.1f handoff=%s fault=cancelled"
        " recovery_ms=%.2f fresh=%s application_checkpoint=%s"
        % (args.sessions, len(peers), healthy_ms, route, recovery_ms, fresh, recovered),
        flush=True,
    )


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("backend", choices=("asyncio", "pybeam"))
    parser.add_argument("--sessions", type=int, default=128)
    args = parser.parse_args()
    if not 3 <= args.sessions <= 1024:
        parser.error("--sessions must be between 3 and 1024")
    return args


if __name__ == "__main__":
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    if arguments().backend == "asyncio":
        asyncio.run(plain_main())
    else:
        run(beam_main)
