# Finite GenStage sample

Consume a fixed sample of transformed messages. In `stream(2, uppercase)`, `2` is the number of sample requests. The count must be 1 to 16. This API supplies its own `héllo` requests; it does not accept your records.

Run this sample from the macOS arm64 `python-beam` wheel. On Linux wheels, which omit the GenStage payload, `stream` raises `BootstrapError`.

```python
# genstage_transform.py
from dataclasses import dataclass
from typing import Literal

from pybeam.codec import Json


@dataclass(frozen=True, slots=True, kw_only=True)
class Work:
    id: int
    action: Literal["ok"]
    text: str


def uppercase(payload: bytes) -> bytes:
    work = Json(Work).decode(payload)
    return work.text.upper().encode("utf-8")
```

```python
# genstage.py
from genstage_transform import uppercase

import pybeam
from pybeam.integrations.genstage.pipeline import stream


async def main() -> None:
    async with stream(2, uppercase) as results:
        async for payload in results:
            print(payload.decode("utf-8"))


if __name__ == "__main__":
    pybeam.run(main)
```

```console
$ uv run genstage.py
HÉLLO
HÉLLO
```

The callback parses each request and returns transformed bytes.

One request is admitted at a time, including running callback work. Pulling the next result permits more work. Context exit waits for graph cleanup. Requests and results are each limited to 1024 encoded bytes.
