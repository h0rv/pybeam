from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pybeam.mailbox import MailboxPolicy, UnboundedMailbox
from pybeam.supervision.config import (
    NativeChildSpec,
    SupervisorConfig,
    validate_max_children,
)
from pybeam.supervision.errors import CapabilityUnavailable

if TYPE_CHECKING:
    from _erlang_impl._supervision import ChildSnapshot, NativeSupervisor


def require_unbounded(policy: MailboxPolicy) -> None:
    if not isinstance(policy, UnboundedMailbox):
        raise CapabilityUnavailable(
            "native queued-plus-running count and byte admission is unavailable"
        )


@dataclass(frozen=True, slots=True)
class SupervisorRef:
    """An explicit reference to an application-owned, local OTP supervisor.

    Operations target logical child IDs, not an observed process generation.
    Inspection is a snapshot. No operation is automatically retried. Timeout or
    cancellation can leave its outcome unknown; inspect before acting again.
    Bounded policies fail closed until native admission is installed.
    """

    _process: object
    mailbox: MailboxPolicy = field(kw_only=True)
    max_children: int | None = field(kw_only=True)

    def __post_init__(self) -> None:
        validate_max_children(self.max_children)

    @classmethod
    def attach(
        cls, process: object, *, mailbox: MailboxPolicy, max_children: int | None
    ) -> SupervisorRef:
        """Declare limits for a PID supplied by the native application owner.

        This does not create or own a root supervisor. A stale supervisor PID
        fails; it is never resolved to a replacement by name. None explicitly
        accepts no child-count bound. A finite limit is not yet enforceable.
        """
        return cls(process, mailbox=mailbox, max_children=max_children)

    def _native(self) -> NativeSupervisor:
        require_unbounded(self.mailbox)
        if self.max_children is not None:
            raise CapabilityUnavailable("atomic native maximum-child admission is unavailable")
        from _erlang_impl._supervision import NativeSupervisor

        return NativeSupervisor(self._process, capacity=None, max_bytes=None, max_children=None)

    async def start_supervisor(self, child_id: bytes, config: SupervisorConfig) -> SupervisorRef:
        """Create a permanent subtree using OTP's required infinite shutdown.

        Finite worker shutdowns bound each worker, not total tree shutdown.
        The subtree policy is separate from this parent's control policy.
        """
        require_unbounded(config.mailbox)
        if config.max_children is not None:
            raise CapabilityUnavailable("atomic native maximum-child admission is unavailable")
        native = await self._native().start_supervisor(
            child_id,
            config.strategy.value,
            config.budget.max_restarts,
            config.budget.window_seconds,
            capacity=None,
            max_bytes=None,
            max_children=None,
        )
        return SupervisorRef(
            native.process, mailbox=config.mailbox, max_children=config.max_children
        )

    async def start_child(self, spec: NativeChildSpec) -> ChildSnapshot:
        """Return a startup snapshot, not a durable process identity.

        The child mailbox is an independent admission scope. Bounded children
        cannot start until their native worker gate is connected. An MFA
        returning ignore produces a stopped snapshot with no PID.
        """
        require_unbounded(spec.mailbox)
        return await self._native().start_child(
            spec.child_id,
            spec.module,
            spec.function,
            spec.args,
            spec.restart.value,
            spec.shutdown.grace_ms,
            capacity=None,
            max_bytes=None,
        )

    async def children(self) -> tuple[ChildSnapshot, ...]:
        return await self._native().children()

    async def stop_child(self, child_id: bytes) -> None:
        """Terminate the current logical child without automatic restart.

        Temporary children lose their specification. Others retain it until
        delete_child, allowing an explicit restart_child with fresh state.
        """
        await self._native().stop_child(child_id)

    async def restart_child(self, child_id: bytes) -> ChildSnapshot:
        """Restart a stopped specification. Running children are not replaced."""
        return await self._native().restart_child(child_id)

    async def delete_child(self, child_id: bytes) -> None:
        """Delete a stopped specification. OTP rejects a running child."""
        await self._native().delete_child(child_id)
