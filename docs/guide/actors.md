# Actors

Use an actor to keep state that many tasks update.

```python
# actors.py
import pybeam


@pybeam.actor
class Counter:
    value: int = 0

    async def add(self, amount: int) -> int:
        self.value += amount
        return self.value

    async def read(self) -> int:
        return self.value


async def main() -> None:
    async with pybeam.ActorGroup() as actors:
        counter = await actors.spawn(Counter, value=10)

        print(await counter.add(2))

        print(await counter.read())


if __name__ == "__main__":
    pybeam.run(main)
```

```console
$ uv run actors.py
12
12
```

`spawn` creates the actor with `value=10`. `add` changes its state and `read` returns it. Leaving the `ActorGroup` stops its actors.

An actor runs one method call at a time, even while a method awaits, so concurrent callers never need a lock. Call methods instead of reading `counter.value`: arguments and results are copied.

Each actor accepts a limited number of waiting calls. A timeout stops waiting; it does not undo the call, and retrying can repeat its effects.

To restart an actor that fails, see [Supervision](supervision.md).

## Save explicit checkpoints

Use `await actors.restore(key, Counter)` instead of `spawn` for saved state.
Inside an actor method, `await pybeam.write_state(self)` saves all declared
fields and returns the new version. Fields must be supported by `pybeam.Json`.
The constructor runs on each start; saved fields replace its defaults before
the first call. Unwritten changes are lost after a crash.

Only one restore can own a class/key at a time. Another restore waits until
the owner is stopped or its group exits. Cancelling a waiting restore cancels
its claim. Ownership survives a local actor crash and explicit `rejoin`.
Keep the configured `data_dir` across launches; an empty `replicas` setting
stores snapshots on this node only.

For node-loss recovery, pass the same `replicas` node names to each
`ActorGroup` and run a standby restore for the same class/key on each node.
The replica set must contain at least three nodes, including the local node.
After the owner node dies, one waiting restore takes over its last saved state.
Writes require a majority of replicas and fence stale ownership tokens;
an actor that loses ownership reports `ActorStateOwnershipError` from
`pybeam.actors` (actor calls carry that kind in `ActorCallError`).

This is standby activation, not transparent routing: calls from other nodes do
not follow the actor to its new owner. In-memory changes since the last write
are lost. Recovery has been tested only with same-host process kills, not
network partitions or host/disk loss.
