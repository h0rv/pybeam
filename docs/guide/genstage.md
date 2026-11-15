# GenStage

Transform requests under demand and consume the results with async iteration.

Save the callback as `genstage_transform.py`.

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Work(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    id: int
    action: Literal["ok"]
    text: str


def uppercase(payload: bytes) -> bytes:
    # Parse one request before using its fields.
    work = Work.model_validate_json(payload)
    return work.text.upper().encode("utf-8")
```

Run this entry file from the same directory.

```python
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

This prints `HÉLLO` twice. This finite graph supplies sample requests containing `héllo`. The callback parses each request and returns transformed bytes.

One request is admitted at a time, including running callback work. Pulling the next result permits more work. Context exit waits for graph cleanup. Requests and results are each limited to 1024 encoded bytes.
