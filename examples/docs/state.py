from pybeam.integrations.hornbeam.client import Beam
from pybeam.integrations.hornbeam.runtime import run


async def main() -> None:
    beam = await Beam.connect()
    store = beam.store("documents")
    await store.set("status", b"draft")
    print(await store.get("status"))
    await store.set("status", b"published")
    print(await store.get("status"))
    print(await beam.incr("visits", 1, namespace="documents"))
    print(await beam.incr("visits", 2, namespace="documents"))
    await store.delete("status")


if __name__ == "__main__":
    run(main)
