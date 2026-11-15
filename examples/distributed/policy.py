from typing import Final

from benchmarks.contract import Budget
from pybeam.runtime.config import Config
from pybeam.runtime.guardian import ManagedStop

MAX_VMS: Final = 2
PROCESS_LIMIT: Final = 16
SCHEDULERS: Final = 1
DIRTY_CPU: Final = 1
DIRTY_IO: Final = 10
ASYNC_WORKERS: Final = 1
ERTS_SUPPORT: Final = 5
MAIN_CONTEXTS: Final = 1
ADMITTED_TASKS: Final = 2
VM_BASE_THREADS: Final = (
    SCHEDULERS + DIRTY_CPU + DIRTY_IO + ASYNC_WORKERS + ERTS_SUPPORT + MAIN_CONTEXTS
)
CONTEXTS_PER_REQUEST: Final = 2
THREAD_LIMIT: Final = (
    MAX_VMS * VM_BASE_THREADS + ADMITTED_TASKS * CONTEXTS_PER_REQUEST + (PROCESS_LIMIT - MAX_VMS)
)


def walkthrough_stop() -> ManagedStop:
    return ManagedStop(node_shutdown_seconds=Config().shutdown_timeout)


def walkthrough_budget() -> Budget:
    """Budget the fixed two-node worker slice, not arbitrary Python task factories.

    OTP29.1.2 with the launcher has 18 ERTS threads: 13 configured workers
    and five support threads. Each VM adds one main Python context. Only B runs
    recipient jobs: its two admitted requests each own a control context and a
    Task context. A is sender-only. Every other allowed process gets one thread.
    This is a sampled watchdog limit, not a native allocation guarantee.
    """
    return Budget(
        wall_seconds=120.0,
        artifact_bytes=2_097_152,
        processes=PROCESS_LIMIT,
        threads=THREAD_LIMIT,
    )
