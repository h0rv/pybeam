import argparse

import uvicorn

from examples.modes_lab.app import app


def main() -> None:
    parser = argparse.ArgumentParser(description="PyBEAM execution modes lab")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=38941)
    arguments = parser.parse_args()
    uvicorn.run(app, host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()
