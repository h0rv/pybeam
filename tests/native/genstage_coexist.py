from genstage_callbacks import process


def blocked(payload: bytes) -> bytes:
    import erlang

    assert erlang.call("genstage_coexist_block") == "ok"
    return process(payload)
