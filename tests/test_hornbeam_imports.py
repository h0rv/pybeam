from pybeam.integrations.hornbeam import Beam, run
from pybeam.integrations.hornbeam.client import Beam as IntegrationBeam
from pybeam.integrations.hornbeam.runtime import run as integration_run


def test_public_hornbeam__reexports_implementation() -> None:
    assert Beam is IntegrationBeam
    assert run is integration_run
