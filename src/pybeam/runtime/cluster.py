import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self

from pybeam.codec import Json
from pybeam.runtime.assets import RuntimeAssets, discover_assets
from pybeam.runtime.config import BootstrapError, Config, Distribution
from pybeam.runtime.guardian import (
    ENDPOINT,
    GUARDIAN_EXIT,
    GuardianEndpoint,
    ManagedStop,
    spawn_owned,
)
from pybeam.runtime.ownership import (
    IDENTITY,
    REGISTRY,
    Identity,
    Registry,
    cleanup,
    durable_write,
    identity,
    record,
    resolve,
)
from pybeam.runtime.process import launch
from pybeam.runtime.startup import STARTUP_PLAN, LauncherEntry, StartupPlan


@dataclass(frozen=True, slots=True, kw_only=True)
class _NodeLaunch:
    entry: Path
    config: Config
    options: tuple[str, ...]
    environment: dict[str, str]


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeExit:
    alias: str
    launcher: Identity
    clean: bool
    status: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedCleanupAck:
    owner: Identity
    guardian: Identity
    cleanup: Registry
    launchers: tuple[Identity, ...]
    shutdowns: tuple[NodeExit, ...]
    forced_cleanup_count: int

    def __post_init__(self) -> None:
        if self.owner == self.guardian:
            raise ValueError("Managed cleanup requires distinct owner and guardian births")
        if self.cleanup.phase != "confirmed" or self.forced_cleanup_count != 0:
            raise ValueError("Managed cleanup was uncertain or forced")
        if (
            len(set(self.launchers)) != len(self.launchers)
            or {item.launcher for item in self.shutdowns} != set(self.launchers)
            or len(self.shutdowns) != len(self.launchers)
            or any(not item.clean for item in self.shutdowns)
            or not set(self.launchers).issubset(self.cleanup.owned)
        ):
            raise ValueError("Managed cleanup lacks an exact launcher/VM ACK")


_NODE_LAUNCH = Json(_NodeLaunch)
NODE_EXIT = Json(NodeExit)
CLEANUP_ACK = Json(ManagedCleanupAck)


class _Phase(StrEnum):
    NEW = "new"
    STARTING = "starting"
    ACTIVE = "active"
    CLOSED = "closed"


class LocalCluster:
    """Own exactly two local VMs and a foreground private EPMD for at most 120 seconds.

    A stopped node is never replayed automatically. restart() starts fresh application state.
    The context must be entered from the main thread. This is a local-source experimental API.
    """

    def __init__(
        self,
        distribution: Distribution,
        *,
        config: Config | None = None,
        guardian: GuardianEndpoint | None = None,
    ) -> None:
        self.distribution = distribution
        self.config = Config() if config is None else config
        self._external_guardian = guardian
        if (
            guardian is not None
            and isinstance(guardian.stop_policy, ManagedStop)
            and self.config.shutdown_timeout != guardian.stop_policy.node_shutdown_seconds
        ):
            raise BootstrapError("Managed stop policy differs from the node cleanup budget")
        self._owner_identity = identity(os.getpid())
        self._launcher_history: list[Identity] = []
        self._forced_cleanup_count = 0
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._guardian: subprocess.Popen[bytes] | None = None
        self._guardian_identity: Identity | None = None
        self._launchers: dict[str, Identity] = {}
        self._launch_counts: dict[str, int] = {}
        self._phase = _Phase.NEW
        self._assets: RuntimeAssets | None = None
        self._directory: Path | None = None
        self.nodes: dict[str, Identity] = {}
        self.names: dict[str, str] = {}
        self.identities: list[Identity] = []
        self.cleanup: Registry | None = None
        self.shutdowns: list[NodeExit] = []

    @property
    def directory(self) -> Path:
        if self._directory is None:
            raise RuntimeError("Enter the cluster context before accessing its directory")
        return self._directory

    def __enter__(self) -> Self:
        if self._phase is not _Phase.NEW:
            raise RuntimeError("A cluster context can be entered only once")
        if threading.current_thread() is not threading.main_thread():
            raise BootstrapError("LocalCluster must start from the main Python thread")
        self._phase = _Phase.STARTING
        try:
            self._assets = discover_assets(self.config)
            epmd = (
                self._assets.bindir / "epmd"
                if self._assets.bindir is not None
                else self._assets.executable.resolve().with_name("epmd")
            )
            if not epmd.is_file():
                raise BootstrapError(
                    "Local distribution requires EPMD beside the selected OTP executable"
                )
            for port in (
                self.distribution.discovery_port,
                *(node.port for node in self.distribution.nodes),
            ):
                with socket.socket() as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind(("127.0.0.1", port))
            self._temporary = tempfile.TemporaryDirectory(prefix="pybeam-distribution-")
            self._directory = Path(self._temporary.name)
            self.directory.chmod(0o700)
            home = self.directory / "home"
            home.mkdir(mode=0o700)
            cookie = home / ".erlang.cookie"
            descriptor = os.open(cookie, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_hex(32))
            environment = dict(os.environ)
            environment.pop("PYBEAM_OUTER_GUARDIAN", None)
            if self._external_guardian is not None:
                self._external_guardian.require_active()
                environment["PYBEAM_OUTER_GUARDIAN"] = ENDPOINT.encode(
                    self._external_guardian
                ).decode()
            environment.update(
                HOME=str(home),
                ERL_EPMD_PORT=str(self.distribution.discovery_port),
                ERL_EPMD_ADDRESS="127.0.0.1",
                PYBEAM_OWNER_DIRECTORY=str(self.directory),
            )
            # Ignore inherited Erlang flags; they may override cookie, ports, or startup.
            for key in ("ERL_FLAGS", "ERL_AFLAGS", "ERL_ZFLAGS"):
                environment.pop(key, None)
            durable_write(
                self.directory / "startup-plan.json",
                STARTUP_PLAN.encode(
                    StartupPlan(aliases=tuple(node.alias for node in self.distribution.nodes))
                ).decode(),
            )
            self._guardian = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    "-m",
                    "pybeam.runtime.guardian",
                    str(self.directory),
                    IDENTITY.encode(identity(os.getpid())).decode(),
                ],
                env=environment,
                start_new_session=True,
            )
            self._guardian_identity = identity(self._guardian.pid)
            deadline = time.monotonic() + 5
            while not (self.directory / "ownership.json").exists():
                if self._guardian.poll() is not None or time.monotonic() > deadline:
                    raise BootstrapError("Private guardian did not start")
                time.sleep(0.01)
            self.identities.append(
                spawn_owned(
                    self.directory,
                    (
                        str(epmd),
                        "-port",
                        str(self.distribution.discovery_port),
                        "-address",
                        "127.0.0.1",
                    ),
                )
            )
            suffix = secrets.token_hex(4)
            self.names = {
                node.alias: "pybeam_" + node.alias + "_" + suffix + "@127.0.0.1"
                for node in self.distribution.nodes
            }
            native = self.directory / "distribution.json"
            native.write_text(
                json.dumps(
                    {
                        "nodes": self.names,
                        "capacity": self.distribution.capacity,
                        "max_bytes": self.distribution.max_bytes,
                    }
                )
            )
            for node in self.distribution.nodes:
                descriptor = _NodeLaunch(
                    entry=node.entry.resolve(),
                    config=replace(self.config, distribution=False),
                    options=(
                        "-name",
                        self.names[node.alias],
                        "-start_epmd",
                        "false",
                        "-kernel",
                        "dist_auto_connect",
                        "never",
                        "inet_dist_use_interface",
                        "{127,0,0,1}",
                        "inet_dist_listen_min",
                        str(node.port),
                        "inet_dist_listen_max",
                        str(node.port),
                    ),
                    environment={
                        "PYBEAM_DISTRIBUTION": str(native),
                        "PYBEAM_CLUSTER_DIRECTORY": str(self.directory),
                        "PYBEAM_NODE_ALIAS": node.alias,
                        "PYBEAM_CLUSTER_READY": str(
                            self.directory / (node.alias + ".started.json")
                        ),
                    },
                )
                (self.directory / (node.alias + ".launch.json")).write_text(
                    _NODE_LAUNCH.encode(descriptor).decode()
                )
                self.restart(node.alias)
            self._phase = _Phase.ACTIVE
            return self
        except BaseException:
            self.close()
            raise

    def restart(self, alias: str) -> Identity:
        if self._phase not in {_Phase.STARTING, _Phase.ACTIVE}:
            raise RuntimeError("Enter the cluster context before restarting a node")
        if self._launch_counts.get(alias, 0) >= 2:
            raise RuntimeError("The bounded local slice permits one restart per configured node")
        if alias not in {node.alias for node in self.distribution.nodes}:
            raise ValueError("Unknown configured node alias")
        previous = self._launchers.get(alias)
        if previous is not None and resolve(previous) is not None:
            raise RuntimeError("Stop the existing owned node before restarting")
        self._launch_counts[alias] = self._launch_counts.get(alias, 0) + 1
        ready = self.directory / (alias + ".started.json")
        ready.unlink(missing_ok=True)
        (self.directory / (alias + ".stopped.json")).unlink(missing_ok=True)
        value = spawn_owned(
            self.directory,
            (
                sys.executable,
                "-m",
                "pybeam.runtime.cluster",
                str(self.directory / (alias + ".launch.json")),
            ),
            entry=LauncherEntry(alias=alias),
        )
        self._launchers[alias] = value
        self._launcher_history.append(value)
        self.identities.append(value)
        deadline = time.monotonic() + self.config.startup_timeout
        while not ready.exists():
            self.check_running(alias)
            if resolve(value) is None or time.monotonic() > deadline:
                raise BootstrapError("Owned node did not acknowledge managed startup: " + alias)
            time.sleep(0.02)
        self.nodes[alias] = IDENTITY.decode(ready.read_text())
        self.identities.append(self.nodes[alias])
        return self.nodes[alias]

    def check_running(self, alias: str = "a") -> None:
        if alias not in {node.alias for node in self.distribution.nodes}:
            raise ValueError("Unknown configured node alias")
        marker = self.directory / (alias + ".stopped.json")
        if not marker.exists():
            return
        outcome = NODE_EXIT.decode(marker.read_bytes())
        message = "Node " + alias.upper() + " exited with status " + str(outcome.status)
        if outcome.status != 0:
            try:
                with (self.directory / (alias + ".stderr.log")).open("rb") as stream:
                    stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, stream.tell() - 4096))
                    tail = stream.read(4096).decode("utf-8", errors="replace")
            except OSError as error:
                tail = "Captured stderr unavailable: " + str(error)
            message += "\nCaptured stderr (last 4096 bytes):\n" + (tail or "(empty)")
        raise BootstrapError(message)

    def stop(self, alias: str) -> None:
        if self._phase not in {_Phase.STARTING, _Phase.ACTIVE}:
            raise RuntimeError("No active cluster owns this node")
        value = self._launchers.get(alias)
        if value is None:
            raise ValueError("Unknown started node alias")
        if resolve(value) is not None:
            with suppress(ProcessLookupError):
                os.kill(value.pid, signal.SIGTERM)
            deadline = time.monotonic() + self.config.shutdown_timeout + 2
            while resolve(value) is not None:
                if time.monotonic() > deadline:
                    raise BootstrapError("Owned node failed graceful stop")
                time.sleep(0.02)
        marker = self.directory / (alias + ".stopped.json")
        if not marker.exists():
            raise BootstrapError("Owned launcher did not acknowledge VM cleanup: " + alias)
        outcome = NODE_EXIT.decode(marker.read_text())
        if outcome.alias != alias or outcome.launcher != value or not outcome.clean:
            if outcome.status != 0:
                self.check_running(alias)
            raise BootstrapError("Owned VM cleanup failed: " + alias)
        if outcome not in self.shutdowns:
            self.shutdowns.append(outcome)

    def close(self) -> None:
        if self._phase is _Phase.CLOSED:
            return
        if self._phase is _Phase.NEW:
            self._phase = _Phase.CLOSED
            return
        failure: BootstrapError | None = None
        try:
            for alias in tuple(self._launchers):
                try:
                    self.stop(alias)
                except BootstrapError as error:
                    failure = error
        finally:
            self._finish_guardian()
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None
            self._phase = _Phase.CLOSED
        if failure is not None:
            raise failure
        endpoint = self._external_guardian
        if (
            endpoint is not None
            and isinstance(endpoint.stop_policy, ManagedStop)
            and self.cleanup is not None
            and self._guardian_identity is not None
        ):
            ack = ManagedCleanupAck(
                owner=self._owner_identity,
                guardian=self._guardian_identity,
                cleanup=self.cleanup,
                launchers=tuple(self._launcher_history),
                shutdowns=tuple(self.shutdowns),
                forced_cleanup_count=self._forced_cleanup_count,
            )
            durable_write(
                endpoint.directory / "managed-cleanup.json", CLEANUP_ACK.encode(ack).decode()
            )

    def _finish_guardian(self) -> None:
        guardian = self._guardian
        if guardian is None:
            return
        (self.directory / "stop").touch()
        timed_out = False
        try:
            guardian.wait(timeout=8)
        except subprocess.TimeoutExpired:
            timed_out = True
        journal = self.directory / "ownership.json"
        registry = (
            REGISTRY.decode(journal.read_text())
            if journal.exists()
            else Registry(phase="spawning" if self.identities else "tracking", owned=())
        )
        owned = set(registry.owned) | set(self.identities)
        if timed_out and self._guardian_identity is not None:
            owned.add(self._guardian_identity)
        if (
            timed_out
            or registry.phase != "confirmed"
            or any(resolve(value) is not None for value in owned)
        ):
            self._forced_cleanup_count += sum(resolve(value) is not None for value in owned)
            survivors = cleanup(owned)
            guardian.wait(timeout=1)
            unknown_spawn = registry.phase == "spawning"
            record(
                self.directory,
                owned,
                "spawning" if unknown_spawn else "uncertain" if survivors else "confirmed",
            )
        self.cleanup = REGISTRY.decode(journal.read_text())
        if self.cleanup.phase != "confirmed":
            raise BootstrapError("Cluster ownership cleanup is uncertain")
        endpoint = self._external_guardian
        if endpoint is not None and isinstance(endpoint.stop_policy, ManagedStop):
            report = GUARDIAN_EXIT.decode((self.directory / "guardian-exit.json").read_bytes())
            if report.registry != self.cleanup:
                raise BootstrapError("Managed guardian cleanup journal differs")
            self._forced_cleanup_count += report.forced_cleanup_count
        self._guardian = None

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


def _node(path: Path) -> None:
    descriptor = _NODE_LAUNCH.decode(path.read_text())
    directory = Path(descriptor.environment["PYBEAM_CLUSTER_DIRECTORY"])
    alias = descriptor.environment["PYBEAM_NODE_ALIAS"]
    launcher = identity(os.getpid())
    try:
        status = launch(
            descriptor.entry,
            [],
            descriptor.config,
            discover_assets(descriptor.config),
            options=descriptor.options,
            extra_environment=descriptor.environment,
        )
    except BootstrapError:
        traceback.print_exc()
        sys.stderr.flush()
        durable_write(
            directory / (alias + ".stopped.json"),
            NODE_EXIT.encode(
                NodeExit(alias=alias, launcher=launcher, clean=False, status=1)
            ).decode(),
        )
        raise SystemExit(1) from None
    durable_write(
        directory / (alias + ".stopped.json"),
        NODE_EXIT.encode(
            NodeExit(alias=alias, launcher=launcher, clean=True, status=status)
        ).decode(),
    )
    raise SystemExit(status)


if __name__ == "__main__":
    _node(Path(sys.argv[1]))
