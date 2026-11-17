# hello.py
import pybeam


async def main() -> None:
    print("Hello from Python")


if __name__ == "__main__":
    pybeam.run(main)
