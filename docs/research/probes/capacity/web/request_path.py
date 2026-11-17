import asyncio
import json
import sys
import threading
import time

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class Item(BaseModel):
    sku: str
    quantity: int
    price: float


class Order(BaseModel):
    customer: str
    items: list[Item]


class Receipt(BaseModel):
    customer: str
    lines: int
    total: float


@app.post("/orders")
def create(order: Order) -> Receipt:
    total = sum(item.quantity * item.price for item in order.items)
    return Receipt(customer=order.customer, lines=len(order.items), total=total)


BODY = json.dumps(
    {
        "customer": "ada",
        "items": [{"sku": "sku" + str(n), "quantity": n, "price": 1.5} for n in range(50)],
    }
).encode()
SCOPE = {
    "type": "http",
    "asgi": {"version": "3.0"},
    "http_version": "1.1",
    "method": "POST",
    "scheme": "http",
    "path": "/orders",
    "raw_path": b"/orders",
    "query_string": b"",
    "root_path": "",
    "headers": [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(BODY)).encode()),
    ],
    "client": ("127.0.0.1", 1),
    "server": ("127.0.0.1", 80),
}


async def request() -> int:
    sent = False
    status = 0

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            await asyncio.sleep(3600)
        sent = True
        return {"type": "http.request", "body": BODY, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            code = message["status"]
            if isinstance(code, int):
                status = code

    await app(dict(SCOPE), receive, send)
    return status


async def serve(count: int) -> None:
    for _ in range(count):
        if await request() != 200:
            raise RuntimeError("Request failed")


def run(threads: int, count: int) -> float:
    errors: list[BaseException] = []

    def work() -> None:
        try:
            asyncio.run(serve(count))
        except Exception as error:
            errors.append(error)

    workers = [threading.Thread(target=work) for _ in range(threads)]
    start = time.perf_counter()
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if errors:
        raise ExceptionGroup("Requests failed", errors)
    return threads * count / (time.perf_counter() - start)


def main() -> None:
    run(1, 200)
    build = "3.14" if sys._is_gil_enabled() else "3.14t"
    for threads in (1, 2, 4, 8):
        print(
            json.dumps(
                {"build": build, "threads": threads, "requests_per_s": round(run(threads, 2000))}
            ),
            flush=True,
        )
    print(json.dumps({"build": build, "gil_after": sys._is_gil_enabled()}))


if __name__ == "__main__":
    main()
