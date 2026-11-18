from examples.sqlalchemy.main import main as serve
from pybeam.integrations.hornbeam import run
from tests.fixtures.sqlalchemy_app import app


async def main() -> None:
    await serve(application=app)


if __name__ == "__main__":
    run(main)
