from examples.sqlalchemy.main import main
from pybeam.integrations.hornbeam import run
from tests.fixtures.sqlalchemy_app import app

assert app.router.lifespan_context is not None


if __name__ == "__main__":
    run(main)
