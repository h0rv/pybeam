import os

from pybeam.runtime.entrypoint import run as run_native
from pybeam.runtime.loader import Entry


def run(main: Entry) -> None:
    """Run an owned VM with Hornbeam's supervised services and no HTTP listener.

    The native launcher must support opt-in Hornbeam startup. It owns callback
    registration and stops the services after Python tasks have quiesced. This
    does not start Hornbeam's web server or replace the application's ASGI server.
    """
    variable = "PYBEAM_HORNBEAM_SERVICES"
    previous = os.environ.get(variable)
    os.environ[variable] = "1"
    try:
        run_native(main)
    finally:
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous
