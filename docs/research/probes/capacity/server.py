# server.py
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import override


class Slow(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        time.sleep(0.05)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass


ThreadingHTTPServer.request_queue_size = 1024
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Slow).serve_forever()
