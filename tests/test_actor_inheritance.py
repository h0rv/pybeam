import pytest

import pybeam
from tests.fixtures.actor_inheritance import (
    Combined,
    Reversed,
    Unrelated,
    call_unbound,
    verify_aliases,
    verify_diamond,
    verify_multiple_inheritance,
    verify_override,
    verify_stopped,
)
from tests.test_pythonic_actors import ScriptedGroup
from tests.test_pythonic_actors import backend as backend


@pytest.mark.parametrize("cls", [Combined, Reversed])
async def test_unbound_methods__resolve_implementation_not_base_local_position(
    backend: ScriptedGroup, cls: type[Combined] | type[Reversed]
) -> None:
    async with pybeam.ActorGroup() as actors:
        await verify_multiple_inheritance(actors, cls)


async def test_unbound_override__preserves_explicit_base_implementation(
    backend: ScriptedGroup,
) -> None:
    async with pybeam.ActorGroup() as actors:
        await verify_override(actors)


async def test_diamond__preserves_mro_and_inline_super(backend: ScriptedGroup) -> None:
    async with pybeam.ActorGroup() as actors:
        await verify_diamond(actors)


async def test_aliases__preserve_original_implementation_identity(backend: ScriptedGroup) -> None:
    async with pybeam.ActorGroup() as actors:
        await verify_aliases(actors)


async def test_unbound_methods__reject_unregistered_identity_before_enqueue(
    backend: ScriptedGroup,
) -> None:
    async with pybeam.ActorGroup() as actors:
        reference = await actors.spawn(Combined)
        native = backend.actors[0]
        native.admitted.clear()
        with pytest.raises(ValueError, match="implementation is not registered"):
            await call_unbound(Unrelated.unrelated, reference)
        assert not native.admitted.is_set()
        assert await reference.snapshot() == (0, 0)


async def test_unbound_methods__reject_stopped_refs_even_without_state_reads(
    backend: ScriptedGroup,
) -> None:
    async with pybeam.ActorGroup() as actors:
        await verify_stopped(actors)
