from collections.abc import Awaitable, Callable
from typing import override

import pybeam


@pybeam.actor
class Ledger:
    a_count: int = 0
    b_count: int = 0

    async def snapshot(self) -> tuple[int, int]:
        return self.a_count, self.b_count

    async def constant(self) -> int:
        return 42


@pybeam.actor
class A(Ledger):
    async def a(self, n: int = 1) -> int:
        self.a_count += n
        return self.a_count


@pybeam.actor
class B(Ledger):
    async def b(self, n: int = 1) -> int:
        self.b_count += n * 10
        return self.b_count


@pybeam.actor
class Combined(A, B):
    pass


@pybeam.actor
class Reversed(B, A):
    pass


@pybeam.actor
class Overridden(A):
    @override
    async def a(self, n: int = 1) -> int:
        self.b_count += n * 7
        return self.b_count

    async def parent(self, n: int = 1) -> int:
        return await super().a(n)


@pybeam.actor
class Root(Ledger):
    async def step(self, n: int = 1) -> int:
        self.a_count += n
        return self.a_count


@pybeam.actor
class Left(Root):
    @override
    async def step(self, n: int = 1) -> int:
        self.b_count += 100
        return await super().step(n + 1)


@pybeam.actor
class Right(Root):
    @override
    async def step(self, n: int = 1) -> int:
        self.b_count += 1000
        return await super().step(n + 10)


@pybeam.actor
class Diamond(Left, Right):
    pass


@pybeam.actor
class ReversedDiamond(Right, Left):
    pass


@pybeam.actor
class InheritedBranch(Root):
    pass


@pybeam.actor
class InheritedDiamond(InheritedBranch, Right):
    pass


@pybeam.actor
class Aliased(A):
    alias = A.a
    another = alias


@pybeam.actor
class LocalAliases(Ledger):
    async def increment(self, n: int = 1) -> int:
        self.a_count += n
        return self.a_count

    alias = increment


@pybeam.actor
class Unrelated(Ledger):
    async def unrelated(self) -> int:
        self.b_count += 999
        return self.b_count


async def call_unbound(method: Callable[..., Awaitable[object]], reference: object) -> object:
    return await method(reference)


async def verify_multiple_inheritance(
    actors: pybeam.ActorGroup, cls: type[Combined] | type[Reversed]
) -> None:
    reference = await actors.spawn(cls)
    assert await A.a(reference, n=2) == 2
    assert await reference.snapshot() == (2, 0)
    assert await B.b(reference, n=3) == 30
    assert await reference.snapshot() == (2, 30)
    assert await reference.a(1) == 3
    assert await reference.b(1) == 40
    assert await reference.snapshot() == (3, 40)


async def verify_override(actors: pybeam.ActorGroup) -> None:
    reference = await actors.spawn(Overridden)
    assert await reference.a(2) == 14
    assert await reference.snapshot() == (0, 14)
    assert await A.a(reference, 3) == 3
    assert await reference.snapshot() == (3, 14)
    assert await reference.parent(4) == 7
    assert await reference.snapshot() == (7, 14)


async def verify_diamond(actors: pybeam.ActorGroup) -> None:
    reference = await actors.spawn(Diamond)
    assert await reference.step(1) == 12
    assert await reference.snapshot() == (12, 1100)
    assert await Left.step(reference, 1) == 24
    assert await reference.snapshot() == (24, 2200)
    assert await Right.step(reference, 1) == 35
    assert await reference.snapshot() == (35, 3200)
    assert await Root.step(reference, 1) == 36
    assert await reference.snapshot() == (36, 3200)
    reversed_reference = await actors.spawn(ReversedDiamond)
    assert await reversed_reference.step(1) == 12
    assert await reversed_reference.snapshot() == (12, 1100)
    assert await Left.step(reversed_reference, 1) == 14
    assert await reversed_reference.snapshot() == (14, 1200)
    assert await Right.step(reversed_reference, 1) == 26
    assert await reversed_reference.snapshot() == (26, 2300)
    assert await Root.step(reversed_reference, 1) == 27
    assert await reversed_reference.snapshot() == (27, 2300)
    inherited = await actors.spawn(InheritedDiamond)
    assert await inherited.step(1) == 11
    assert await inherited.snapshot() == (11, 1000)
    assert await Root.step(inherited, 1) == 12
    assert await inherited.snapshot() == (12, 1000)


async def verify_aliases(actors: pybeam.ActorGroup) -> None:
    inherited = await actors.spawn(Aliased)
    assert await inherited.alias(1) == 1
    assert await inherited.another(2) == 3
    assert await A.a(inherited, 3) == 6
    assert await inherited.a(4) == 10
    assert await inherited.snapshot() == (10, 0)
    local = await actors.spawn(LocalAliases)
    assert await local.alias(1) == 1
    assert await LocalAliases.increment(local, 2) == 3
    assert await LocalAliases.alias(local, 3) == 6
    assert await local.snapshot() == (6, 0)


async def verify_unregistered(actors: pybeam.ActorGroup) -> None:
    reference = await actors.spawn(Combined)
    try:
        await call_unbound(Unrelated.unrelated, reference)
    except ValueError:
        pass
    else:
        raise AssertionError("An unrelated implementation was dispatched")
    assert await reference.snapshot() == (0, 0)


async def expect_stopped(call: Awaitable[object]) -> None:
    try:
        await call
    except pybeam.ActorStoppedError:
        pass
    else:
        raise AssertionError("Stopped unbound implementation executed")


async def verify_stopped(actors: pybeam.ActorGroup) -> None:
    reference = await actors.spawn(Combined)
    await actors.stop(reference)
    await expect_stopped(A.a(reference))
    await expect_stopped(B.b(reference))
    await expect_stopped(Ledger.constant(reference))
    overridden = await actors.spawn(Overridden)
    await actors.stop(overridden)
    await expect_stopped(A.a(overridden))
    await expect_stopped(Overridden.a(overridden))
    await expect_stopped(Ledger.constant(overridden))


async def verify() -> None:
    async with pybeam.ActorGroup() as actors:
        await verify_multiple_inheritance(actors, Combined)
        await verify_multiple_inheritance(actors, Reversed)
        await verify_override(actors)
        await verify_diamond(actors)
        await verify_aliases(actors)
        await verify_unregistered(actors)
        await verify_stopped(actors)
    print("pythonic-inheritance-ok", flush=True)


if __name__ == "__main__":
    pybeam.run(verify)
