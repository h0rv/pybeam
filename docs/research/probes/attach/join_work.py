import asyncio

import erlang
from attach_probe import actors, tasks


async def work(host: object, host_node: object) -> dict[str, str]:
    result = {"actors": await actors(), "tasks": await tasks()}
    result["remote_pid_type"] = type(host).__name__
    try:
        erlang.send(host, (b"push", 42))
    except TypeError as error:
        result["pid_send"] = str(error)
    else:
        result["pid_send"] = "accepted"
    try:
        erlang.send(("probe_receiver", host_node), (b"named_push", 42))
    except TypeError as error:
        result["named_send"] = str(error)
    else:
        result["named_send"] = "accepted"
    await erlang.async_call("probe_push", (b"push", 42))
    return result


async def slow(host: object) -> None:
    await erlang.async_call("probe_push", b"slow_started")
    await asyncio.sleep(300)
