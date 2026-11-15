import asyncio
import secrets
import sys

import pybeam
from pybeam.integrations.hornbeam.client import Beam
from pybeam.integrations.hornbeam.errors import SubscriptionOverflow
from pybeam.integrations.hornbeam.runtime import run


async def main() -> None:
    if "--without-services" in sys.argv:
        try:
            await Beam.connect()
        except RuntimeError as error:
            if "not registered" not in str(error):
                raise
            print("services remain opt-in", flush=True)
            return
        raise AssertionError("Ordinary managed startup unexpectedly enabled Hornbeam")
    services = await Beam.connect()
    baseline = await services.subscription_count()
    async with services.subscribe("discord:probe", max_bytes=1024) as subscription:
        if await services.publish("discord:probe", b"invalidate") != 1:
            raise AssertionError("Expected one native subscription")
        if await subscription.receive() != b"invalidate":
            raise AssertionError("Native invalidation did not arrive")
    if await services.subscription_count() != baseline:
        raise AssertionError("Native subscription did not return to baseline")
    async with services.subscribe("discord:slow", max_bytes=1024) as slow:
        for _ in range(16):
            await services.publish("discord:slow", secrets.token_bytes(512))
        received = 0
        async with asyncio.timeout(2):
            while True:
                try:
                    await slow.receive()
                except SubscriptionOverflow:
                    break
                received += 1
        if received >= 16:
            raise AssertionError("Slow native subscriber did not bound buffered deliveries")
    if await services.subscription_count() != baseline:
        raise AssertionError("Slow subscriber did not retire to baseline")
    print("managed services drained", flush=True)


if __name__ == "__main__":
    if "--without-services" in sys.argv:
        pybeam.run(main)
    else:
        run(main)
