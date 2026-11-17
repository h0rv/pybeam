import pickle

pinned: dict[str, object] = {}


def build(entries: int) -> bytes:
    pinned.clear()
    pinned.update({"key" + str(index): "value" * 4 for index in range(entries)})
    pinned["count"] = 0
    return pickle.dumps(pinned)


def step_pinned() -> int:
    count = pinned["count"]
    if not isinstance(count, int):
        raise TypeError("Invalid count", count)
    pinned["count"] = count + 1
    return count + 1


def step_copy(blob: bytes) -> bytes:
    state = pickle.loads(blob)  # noqa: S301
    state["count"] += 1
    return pickle.dumps(state)


def step_transfer(blob: bytes) -> int:
    return len(blob)
