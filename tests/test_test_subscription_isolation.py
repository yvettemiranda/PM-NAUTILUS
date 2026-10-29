"""A damaged TEST subscriber cannot stop another account's public feed."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pm_nautilus.market import MarketService, PublicMarketHub
from pm_nautilus.test_market import TestMarketHub, create_market_service
from pm_nautilus.test_runtime import TestRuntime
from test_token_storage import token


async def idle(*_args):
    await asyncio.Future()


@asynccontextmanager
async def accounts(path):
    hub = TestMarketHub()
    hub.socket = idle
    runtimes = [TestRuntime(path / str(number) / "state.sqlite") for number in (1, 2)]
    services = []
    try:
        for number, runtime in enumerate(runtimes, 1):
            runtime.add_tokens([token(str(number))])
            runtime.monitored = {str(number)}
            runtime.store.put("status", "RUNNING")
            services.append(create_market_service(runtime, hub=hub))
        yield hub, runtimes, services
    finally:
        for service in services:
            await service.close()
        await hub.close()
        for runtime in runtimes:
            runtime.close()


def fail():
    raise ValueError("controlled single TEST subscription failure")


@pytest.mark.parametrize("phase", ["desired", "invalid-desired", "token", "book"])
def test_subscription_planning_excludes_only_the_broken_test(tmp_path, phase):
    async def run():
        async with accounts(tmp_path) as (hub, runtimes, services):
            bad, healthy = services
            if phase == "desired":
                bad.desired_subscription_ids = fail
            elif phase == "invalid-desired":
                bad.desired_subscription_ids = lambda: None
            elif phase == "token":
                runtimes[0].tokens.pop("1")
            else:
                runtimes[0].books["1"] = object()
            await healthy.sync_subscriptions()
            assert bad.closed and bad not in hub.services
            assert runtimes[0].status == "PAUSED"
            assert bad.scan_status["serviceError"] in {"ValueError", "KeyError", "AttributeError"}
            assert runtimes[1].status == "RUNNING" and not healthy.closed
            assert hub.runtime.monitored == {"2"}
            assert set().union(*hub.active_groups.values()) == {"2"}
            assert bad not in hub._subscription_monitored
            assert hub._subscription_monitored[healthy] == {"2"}
            assert not healthy.scan_status.get("serviceError")
            with pytest.raises(ValueError, match="重新启动程序"):
                bad.start()
            with pytest.raises(ValueError, match="重新启动程序"):
                await bad.start_async()
            assert runtimes[0].status == "PAUSED"
            assert bad.scan_status.get("serviceError")

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["socket", "stream", "delivery"])
def test_socket_diagnostics_and_delivery_isolate_subscription_faults(tmp_path, phase):
    async def run():
        async with accounts(tmp_path) as (hub, runtimes, services):
            bad, healthy = services
            group = ("controlled",)
            hub.active_groups[group] = {"1", "2"}
            hub.streams[group] = {"state": "READY", "error": "controlled stream warning"}
            bad.desired_subscription_ids = fail
            if phase == "socket":
                assert hub.socket_consumers(group) == [healthy]
            elif phase == "stream":
                hub.update_consumer_stream_errors()
                assert healthy.scan_status["streamError"] == "controlled stream warning"
            else:
                received = []
                healthy.message = lambda *_args, **_kwargs: received.append(True)
                # Unknown/overridden consumers deliberately use the original
                # raw delivery path, which must have the same isolation gate.
                hub.message({"event_type": "unrelated"}, {"1", "2"})
                assert received == [True]
            assert bad.closed and bad not in hub.services
            assert runtimes[0].status == "PAUSED"
            assert runtimes[1].status == "RUNNING" and not healthy.closed

    asyncio.run(run())


def test_healthy_service_run_does_not_pause_after_its_sibling_fails(tmp_path):
    async def run():
        async with accounts(tmp_path) as (_hub, runtimes, services):
            bad, healthy = services
            bad.desired_subscription_ids = fail
            healthy._scan_finished_at = asyncio.get_running_loop().time()
            healthy.scan_task = asyncio.create_task(idle())
            task = asyncio.create_task(healthy.run())
            try:
                await asyncio.sleep(0.01)
                assert runtimes[1].status == "RUNNING"
                assert not healthy.scan_status.get("serviceError")
                assert bad.closed and runtimes[0].status == "PAUSED"
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("hub_type", [PublicMarketHub, TestMarketHub])
def test_live_subscription_and_preparation_exceptions_keep_original_path(hub_type):
    async def run():
        hub = hub_type()
        service = object.__new__(MarketService)
        service.runtime = SimpleNamespace(mode="LIVE", tokens={})
        service.closed = False
        service.desired_subscription_ids = fail
        hub.services.add(service)
        hub.consumer_error = Mock()
        try:
            with pytest.raises(ValueError, match="controlled single"):
                hub.consumer_subscription_ids(service)
            with pytest.raises(KeyError):
                hub._prepare_subscription_consumer(service, {"missing"})
            assert not service.closed and service in hub.services
            hub.consumer_error.assert_not_called()
        finally:
            hub.services.clear()
            await hub.close()

    asyncio.run(run())
