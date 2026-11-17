import asyncio
import os

from pyview.events import InfoEvent

from pybeam import run
from pybeam.distribution import Request, local, remote
from pybeam.integrations.hornbeam import Beam
from pybeam.integrations.pyview.subscriptions import ViewSubscription

TOPIC = "pyview:cross-node"


def notification(payload: bytes) -> InfoEvent:
    return InfoEvent(TOPIC, payload.hex())


class Receiver:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer

    async def send_info(self, event: InfoEvent) -> None:
        self.writer.write(("event:" + str(event.payload) + "\n").encode())
        await self.writer.drain()


async def main() -> None:
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", int(os.environ["PYBEAM_PUBSUB_CONTROL_PORT"])
    )
    beam = await Beam.connect()
    alias = os.environ["PYBEAM_NODE_ALIAS"]
    view = await ViewSubscription.start(beam, Receiver(writer), TOPIC, notification)
    try:
        writer.write((alias + "\n").encode())
        await writer.drain()
        async with asyncio.timeout(20):
            while line := await reader.readline():
                command = line.strip()
                if command == b"stop":
                    break
                if command == b"connect":
                    assert alias == "a"
                    assert (await local(Request(op="connect", alias="b"))).status == "ok"
                    assert (await remote("b", Request(op="snapshot"))).status == "ok"
                    writer.write(b"connected\n")
                elif command == b"lost":
                    assert alias == "a"
                    assert (await local(Request(op="snapshot"))).nodes == ()
                    writer.write(b"disconnected\n")
                else:
                    assert alias == "a"
                    count = await beam.publish(TOPIC, bytes.fromhex(command.decode()))
                    writer.write(("published:" + str(count) + "\n").encode())
                await writer.drain()
    finally:
        await view.aclose()
        assert await beam.subscription_count() == 0
        writer.write(b"closed\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()


run(main)
