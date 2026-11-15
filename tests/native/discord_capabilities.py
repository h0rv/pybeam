import asyncio

import pybeam
from pybeam.integrations.hornbeam.client import Beam


async def main() -> None:
    from erlang import ErlangEventLoop

    if not isinstance(asyncio.get_running_loop(), ErlangEventLoop):
        raise RuntimeError("Capability probe requires the managed native loop")
    await Beam.connect()
    print("native-pubsub-owner-ok")


if __name__ == "__main__":
    pybeam.run(main)
