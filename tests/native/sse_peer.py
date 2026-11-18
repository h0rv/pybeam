import http.client
import sys


def main() -> None:
    connection = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]))
    try:
        connection.request("GET", "/events?scoped=" + sys.argv[2])
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(response.status)
        if connection.sock is None:
            raise RuntimeError("stream has no socket")
        port = connection.sock.getsockname()[1]
        response.read1(4096)
        print(port, flush=True)
        while response.read1(4096):
            pass
    finally:
        connection.close()


if __name__ == "__main__":
    main()
