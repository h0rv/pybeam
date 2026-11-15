from typing import Final

from benchmarks.contract import Budget

MAX_VMS: Final = 2
PROCESS_LIMIT: Final = 16
SCHEDULERS: Final = 1
DIRTY_CPU: Final = 1
DIRTY_IO: Final = 10
ASYNC_WORKERS: Final = 1
ERTS_SUPPORT: Final = 5
MAIN_CONTEXTS: Final = 1
ADMITTED_TASKS: Final = 2
VM_THREADS: Final = (
    SCHEDULERS
    + DIRTY_CPU
    + DIRTY_IO
    + ASYNC_WORKERS
    + ERTS_SUPPORT
    + MAIN_CONTEXTS
    + ADMITTED_TASKS
)
THREAD_LIMIT: Final = MAX_VMS * VM_THREADS + (PROCESS_LIMIT - MAX_VMS)


def walkthrough_budget() -> Budget:
    """Budget the fixed two-node worker slice, not arbitrary Python task factories.

    OTP29.1.2 with the launcher has 18 ERTS threads: 13 configured workers
    and five support threads. Each VM adds one main Python context and at most
    two request-owned Task contexts. Every other allowed process gets one thread.
    This is a sampled watchdog limit, not a native allocation guarantee.
    """
    return Budget(
        wall_seconds=120.0,
        artifact_bytes=2_097_152,
        processes=PROCESS_LIMIT,
        threads=THREAD_LIMIT,
    )
