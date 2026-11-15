# Get started

```python
import pybeam


async def main() -> None:
    print("Hello from Python")


if __name__ == "__main__":
    pybeam.run(main)
```

Save this as `main.py` and run `python main.py`. It prints `Hello from Python`.

Write your application inside `async def main()`. Pass the function to `pybeam.run`, not `main()`. You do not start or manage a VM yourself.

Next, run [concurrent async Python](runtime.md), create an [actor](actors.md), or [send a message](messaging.md).
