import ipaddress
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass

from pybeam.runtime.config import BootstrapError

PORT = 4370
INTERVAL = 5.0

_JOIN = (
    '{ok,Ip}=inet:parse_address(os:getenv("PYBEAM_CLUSTER_ADDRESS")),'
    "Family=case tuple_size(Ip) of 4->inet;8->inet6 end,"
    'Name=fun(A)->list_to_atom("pybeam@"++inet:ntoa(A)) end,'
    "{ok,_}=net_kernel:start(Name(Ip),#{name_domain=>longnames}),"
    'true=erlang:set_cookie(list_to_atom(os:getenv("PYBEAM_COOKIE"))),'
    'Query=os:getenv("PYBEAM_CLUSTER"),'
    "spawn(fun Join()->"
    "case inet:getaddrs(Query,Family) of"
    " {ok,Peers}->[net_kernel:connect_node(Name(P))||P<-Peers,P=/=Ip];"
    " {error,_}->ok end,"
    "receive after " + str(round(INTERVAL * 1000)) + "->Join() end end)."
)


@dataclass(frozen=True, slots=True)
class Cluster:
    """Join every `pybeam` node that a DNS name resolves to.

    Nodes listen on TCP port 4370 of the address that routes to peers, without EPMD, and trust any peer that presents
    the shared cookie. The name is resolved again every five seconds; nodes that
    disappear from DNS are not disconnected.
    """

    query: str
    address: str

    @property
    def node(self) -> str:
        return "pybeam@" + self.address

    @property
    def options(self) -> tuple[str, ...]:
        address = ipaddress.ip_address(self.address)
        if address.version == 4:
            family: tuple[str, ...] = ()
            words = address.packed
        else:
            family = ("-proto_dist", "inet6_tcp")
            words = tuple(int(word, 16) for word in address.exploded.split(":"))
        interface = "{" + ",".join(str(word) for word in words) + "}"
        return (
            "-start_epmd",
            "false",
            "-erl_epmd_port",
            str(PORT),
            *family,
            "-kernel",
            "inet_dist_use_interface",
            interface,
            "-eval",
            _JOIN,
        )


def _address(query: str) -> str | None:
    try:
        answers = socket.getaddrinfo(query, PORT, type=socket.SOCK_DGRAM)
    except socket.gaierror:
        return None
    # Every node must pick the same family, so prefer IPv4 whenever the name has it.
    answers.sort(key=lambda answer: answer[0] != socket.AF_INET)
    for family, kind, _, _, target in answers:
        with socket.socket(family, kind) as probe:
            try:
                # UDP connect sends nothing; it selects the interface that routes to peers.
                probe.connect(target)
            except OSError:
                continue
            host = probe.getsockname()[0]
        if isinstance(host, str):
            return host
    return None


def cluster(environment: Mapping[str, str], timeout: float) -> Cluster | None:
    query = environment.get("PYBEAM_CLUSTER", "")
    if not query:
        return None
    cookie = environment.get("PYBEAM_COOKIE", "")
    if not cookie or len(cookie) > 255:
        raise BootstrapError("PYBEAM_CLUSTER requires PYBEAM_COOKIE of 1 to 255 characters")
    deadline = time.monotonic() + timeout
    while (address := _address(query)) is None:
        if time.monotonic() >= deadline:
            raise BootstrapError("PYBEAM_CLUSTER does not resolve: " + query)
        time.sleep(1.0)
    return Cluster(query, address)
