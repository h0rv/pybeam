from time import sleep

from examples.genstage_callbacks import process


def busy(payload: bytes) -> bytes:
    import erlang

    erlang.call("owned_started")
    sleep(0.2)
    result = process(payload)
    erlang.call("owned_finished")
    return result


def raises(payload: bytes) -> bytes:
    raise RuntimeError("deliberate Python callback failure")
