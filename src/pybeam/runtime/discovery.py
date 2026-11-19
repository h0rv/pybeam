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

_PEER_JOIN = (
    'true=erlang:set_cookie(list_to_atom(os:getenv("PYBEAM_COOKIE"))),'
    'Peers=[list_to_atom(string:trim(N))||N<-string:split(os:getenv("PYBEAM_CLUSTER"),",",all)],'
    "spawn(fun Join()->"
    "[net_kernel:connect_node(P)||P<-Peers],"
    "receive after " + str(round(INTERVAL * 1000)) + "->Join() end end)."
)


@dataclass(frozen=True, slots=True)
class Cluster:
    """Connect to explicit longnames via EPMD, or discover PyBEAM nodes through DNS.

    DNS nodes listen on TCP port 4370 without EPMD. Both modes trust the shared
    cookie and retry every five seconds; removed DNS peers are not disconnected.
    """

    query: str
    address: str
    peers: tuple[str, ...] = ()

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
        naming = (
            ("-name", self.node)
            if self.peers
            else ("-start_epmd", "false", "-erl_epmd_port", str(PORT))
        )
        return (
            *naming,
            *family,
            "-kernel",
            "inet_dist_use_interface",
            interface,
            "-eval",
            _PEER_JOIN if self.peers else _JOIN,
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
    peers: tuple[str, ...] = ()
    host = query
    if "@" in query:
        peers = tuple(node.strip() for node in query.split(","))
        for peer in peers:
            name, separator, peer_host = peer.partition("@")
            if (
                not name
                or not separator
                or not peer_host
                or "@" in peer_host
                or any(character.isspace() for character in peer)
            ):
                raise BootstrapError("PYBEAM_CLUSTER requires comma-separated longnames: name@host")
        host = peers[0].partition("@")[2]
    deadline = time.monotonic() + timeout
    while (address := _address(host)) is None:
        if time.monotonic() >= deadline:
            raise BootstrapError("PYBEAM_CLUSTER does not resolve: " + query)
        time.sleep(1.0)
    return Cluster(query, address, peers)
