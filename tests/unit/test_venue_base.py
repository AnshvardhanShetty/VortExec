import pytest

from vortexec.venues.base import VenueConnector


def test_venue_connector_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        VenueConnector()  # type: ignore[abstract]


def test_subclass_missing_methods_cannot_be_instantiated() -> None:
    class Partial(VenueConnector):
        async def connect(self) -> None:
            pass

    with pytest.raises(TypeError):
        Partial()  # type: ignore[abstract]
