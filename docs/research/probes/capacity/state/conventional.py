import json
import os
import socket
import statistics
import threading
import time
from collections.abc import Callable
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import override

KV = ("127.0.0.1", int(os.environ.get("PROBE_KV_PORT", "6379")))
ENTRIES = (32, 3300, 33000)


def build(entries: int) -> dict[str, object]:
    state: dict[str, object] = {"key" + str(index): "value" * 4 for index in range(entries)}
    state["count"] = 0
    return state


def step(state: object) -> bytes:
    if not isinstance(state, dict) or not isinstance(count := state.get("count"), int):
        raise TypeError("Invalid state", state)
    state["count"] = count + 1
    return json.dumps(state).encode()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = step(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: object) -> None:
        return


class Resp:
    def __init__(self) -> None:
        self.sock = socket.create_connection(KV)
        self.file = self.sock.makefile("rb")

    def command(self, *parts: bytes) -> bytes | None:
        frame = b"*" + str(len(parts)).encode() + b"\r\n"
        for part in parts:
            frame += b"$" + str(len(part)).encode() + b"\r\n" + part + b"\r\n"
        self.sock.sendall(frame)
        line = self.file.readline()
        if line.startswith(b"$"):
            size = int(line[1:])
            if size < 0:
                return None
            data = self.file.read(size + 2)
            return data[:-2]
        if line.startswith(b"-"):
            raise RuntimeError(line.decode())
        return line


def median_us(operation: Callable[[], object]) -> float:
    operation()
    samples = []
    for _ in range(300):
        start = time.perf_counter_ns()
        operation()
        samples.append(time.perf_counter_ns() - start)
    return round(statistics.median(samples) / 1000, 1)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    http = HTTPConnection("127.0.0.1", server.server_port)
    kv = Resp()
    for entries in ENTRIES:
        blob = json.dumps(build(entries)).encode()
        kv.command(b"SET", b"actor", blob)

        def over_http(body: bytes = blob) -> None:
            http.request("POST", "/", body, {"Content-Type": "application/json"})
            http.getresponse().read()

        def through_kv() -> None:
            data = kv.command(b"GET", b"actor")
            if data is None:
                raise RuntimeError("Missing state")
            kv.command(b"SET", b"actor", step(json.loads(data)))

        row = {
            "json_bytes": len(blob),
            "json_codec_us": median_us(lambda body=blob: step(json.loads(body))),
            "http_json_us": median_us(over_http),
            "kv_json_us": median_us(through_kv),
        }
        print(json.dumps(row), flush=True)
    server.shutdown()


if __name__ == "__main__":
    main()
