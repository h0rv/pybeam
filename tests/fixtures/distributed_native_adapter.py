import pytest

from examples.showcase import distributed
from tests.fixtures.distributed_native_hold import held_demonstration

if __name__ == "__main__":
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(distributed, "demonstration", held_demonstration)
        distributed.main()
