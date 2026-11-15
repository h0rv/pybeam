from collections.abc import Callable

from litestar.background_tasks import BackgroundTask

from pybeam.tasks import TaskFunction, TaskRef, TaskSupervisor


def background_task[Q, R](
    supervisor: TaskSupervisor,
    function: TaskFunction[Q, R],
    request: Q,
    receive: Callable[[TaskRef[R]], None],
) -> BackgroundTask:
    """After response, submit once to the application's open task supervisor.

    The callback waits only for admission and child startup, not the effect.
    Submission errors propagate to Litestar. Later errors remain on TaskRef
    and are reported by supervisor shutdown if the application does not await it.
    """

    async def handoff() -> None:
        receive(await supervisor.start(function, request))

    return BackgroundTask(handoff)
