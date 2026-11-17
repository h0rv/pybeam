# subscriptions.py
from pybeam.integrations.hornbeam import Beam, run


async def main() -> None:
    beam = await Beam.connect()
    store = beam.store("documents")

    await store.set("status", b"draft")

    async with beam.subscribe("documents:changed", max_bytes=4096) as changes:
        await store.set("status", b"published")
        await beam.publish("documents:changed", b"status")

        async for key in changes:
            print(await store.get(key.decode("utf-8")))
            break

    print(await beam.subscription_count())

    await store.delete("status")


if __name__ == "__main__":
    run(main)
