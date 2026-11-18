from typing import TYPE_CHECKING

from pybeam.integrations.hornbeam.client import Beam as Beam
from pybeam.integrations.hornbeam.contracts import ByteStore as ByteStore
from pybeam.integrations.hornbeam.contracts import PresenceSubscription as PresenceSubscription
from pybeam.integrations.hornbeam.contracts import Services as Services
from pybeam.integrations.hornbeam.contracts import Subscription as Subscription
from pybeam.integrations.hornbeam.runtime import run as run

if TYPE_CHECKING:
    from hornbeam.services.presence import PresenceEntry as PresenceEntry


def __getattr__(name: str) -> type[PresenceEntry]:
    if name == "PresenceEntry":
        from hornbeam.services.presence import PresenceEntry

        return PresenceEntry
    raise AttributeError(name)
