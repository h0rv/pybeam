import erlang


def reply(pid: object, node: str) -> int:
    erlang.send(pid, ("pid", 42))
    erlang.send((erlang.atom("join_receiver"), erlang.atom(node)), ("named", 42))
    return 42
