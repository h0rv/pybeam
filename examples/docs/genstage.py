from genstage_transform import uppercase

import pybeam
from pybeam.integrations.genstage.pipeline import stream


async def main() -> None:
    async with stream(2, uppercase) as results:
        async for payload in results:
            print(payload.decode("utf-8"))


if __name__ == "__main__":
    pybeam.run(main)
