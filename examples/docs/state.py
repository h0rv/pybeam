# state.py
from pybeam.hornbeam import Beam, run


async def main() -> None:
    beam = await Beam.connect()
    store = beam.store("documents")

    await store.set("status", b"draft")
    print(await store.get("status"))

    await store.set("status", b"published")
    print(await store.get("status"))

    visits = await beam.incr("visits", 1, namespace="documents")
    print(visits)

    visits = await beam.incr("visits", 2, namespace="documents")
    print(visits)

    await store.delete("status")


if __name__ == "__main__":
    run(main)
