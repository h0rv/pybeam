import pytest
from pyview import Depends
from pyview.binding.binder import Binder
from pyview.binding.context import BindContext
from pyview.binding.params import Params
from pyview.live_socket import UnconnectedSocket
from pyview.pyview import PyView

from pybeam.integrations.hornbeam import Services
from pybeam.integrations.pyview.dependencies import BeamProvider, beam_services, provider
from tests.fakes import FakeServices, ServiceState


async def test_binder_injects_socket_into_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    beam = FakeServices(ServiceState(), owner="app")
    calls = 0

    async def connect() -> Services:
        nonlocal calls
        calls += 1
        return beam

    monkeypatch.setattr(provider, "_connect", connect)
    socket = UnconnectedSocket[int]()

    async def handler(services: Services = Depends(beam_services)) -> None:  # noqa: B008
        assert services is beam

    app = PyView(lifespan=provider.lifespan)
    async with app.router.lifespan_context(app):
        context = BindContext(Params({}), None, None, socket, None)
        result = await Binder[int]().abind(handler, context)
        assert result.success, result.errors
        assert result.bound_args["services"] is beam
        await handler(beam_services(socket))
        with pytest.raises(RuntimeError, match="already running"):
            async with provider.lifespan(app):
                pytest.fail("nested lifespan accepted")
    assert calls == 1
    with pytest.raises(RuntimeError, match="active application lifespan"):
        beam_services(socket)


async def test_failed_startup_can_be_retried() -> None:
    attempts = 0
    beam = FakeServices(ServiceState(), owner="app")

    async def connect() -> Services:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("connection failed")
        return beam

    instance = BeamProvider(connect)
    with pytest.raises(ValueError, match="connection failed"):
        async with instance.lifespan(object()):
            pytest.fail("startup accepted")
    with pytest.raises(RuntimeError, match="active application lifespan"):
        instance(UnconnectedSocket[int]())
    with pytest.raises(ValueError, match="application failed"):
        async with instance.lifespan(object()):
            assert instance.services is beam
            message = "application failed"
            raise ValueError(message)
    with pytest.raises(RuntimeError, match="active application lifespan"):
        _ = instance.services
