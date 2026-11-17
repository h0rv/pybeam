# messaging.py
from pybeam.hornbeam import Beam, run


async def main() -> None:
    beam = await Beam.connect()

    async with beam.subscribe("orders", max_bytes=4096) as inbox:
        await beam.publish("orders", b"order-42")

        message = await inbox.receive()
        print(message.decode("utf-8"))


if __name__ == "__main__":
    run(main)
