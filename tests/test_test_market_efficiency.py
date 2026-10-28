"""TEST subscription-plan reuse with real isolated runtimes and local sockets."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import Mock

import pytest

from pm_nautilus.market import MarketService, PublicMarketHub
from pm_nautilus.strategy import Runtime
from pm_nautilus.test_market import TestMarketHub, TestMarketService, create_market_service
from pm_nautilus.test_runtime import TestRuntime

from test_live_recovery import controlled_runtime
from test_market_efficiency import IdleSocket, public_book, settle_tasks
from test_runtime import setup
from test_lifecycle import controlled_factory


@asynccontextmanager
async def complete_hub(path, monkeypatch, count=2, service_type=MarketService):
    reference, clock, token = setup(path / "reference")
    reference.close()
    sockets = []

    def connect(*args, **kwargs):
        socket = IdleSocket()
        sockets.append(socket)
        return socket

    monkeypatch.setattr("pm_nautilus.market.connect", connect)
    hub = TestMarketHub()
    runtimes, services = [], []
    try:
        for number in range(count):
            runtime = TestRuntime(path / str(number) / "test.sqlite", clock=clock)
            runtime.add_tokens([token])
            runtimes.append(runtime)
            services.append(service_type(runtime, hub=hub))
        await hub.sync_subscriptions()
        await settle_tasks()
        hub.message(public_book(token), {token.token_id})
        for group in hub.streams:
            hub.refresh_stream_readiness(group)
        await hub.sync_subscriptions()
        assert hub._complete_test_plan is not None
        yield hub, runtimes, services, sockets, token, clock
    finally:
        for service in services:
            await service.close()
        await hub.close()
        for runtime in runtimes:
            runtime.close()


def test_complete_ten_account_plan_is_reused_but_additions_subscribe_immediately(
    tmp_path, monkeypatch
):
    async def run():
        async with complete_hub(tmp_path, monkeypatch, count=10) as state:
            hub, runtimes, services, sockets, token, _ = state
            releases = [Mock(wraps=runtime.release_unmonitored) for runtime in runtimes]
            for runtime, release in zip(runtimes, releases):
                monkeypatch.setattr(runtime, "release_unmonitored", release)
            plans = hub.plan_runs
            for _ in range(20):
                await hub.sync_subscriptions()
            assert hub.plan_runs == plans and hub.plan_skips == 20
            assert all(release.call_count == 0 for release in releases)
            assert len(sockets) == 1
            original_task = next(iter(hub.tasks.values()))

            added = replace(token, token_id="2", event_id="new", market_id="new-market")
            runtimes[1].add_tokens([added])
            await services[1].sync_subscriptions()
            assert hub.plan_runs == plans + 1
            assert next(iter(hub.tasks.values())) is original_task
            assert any(
                json.loads(message).get("operation") == "subscribe"
                and "2" in json.loads(message)["assets_ids"]
                for message in sockets[0].sent
                if message.startswith("{")
            )
            assert not runtimes[1].books["2"].ready
            assert all(runtime.books["1"].ready for runtime in runtimes)
            hub.message(public_book(added, timestamp=1001), {"1", "2"})
            for group in hub.streams:
                hub.refresh_stream_readiness(group)
            await hub.sync_subscriptions()
            await hub.sync_subscriptions()
            assert hub.plan_skips == 21 and runtimes[1].books["2"].ready

    asyncio.run(run())


def test_removal_and_real_reset_keep_sibling_socket_and_consumed_depth(tmp_path, monkeypatch):
    async def run():
        async with complete_hub(tmp_path, monkeypatch) as state:
            hub, runtimes, services, sockets, token, clock = state
            first, old = runtimes
            first.start()
            first.pause()
            consumed = dict(first.books["1"].ask.consumed)
            events = first.store.db.execute("SELECT count(*) FROM native_events").fetchone()[0]
            assert consumed
            shared = next(iter(hub.tasks.values()))
            await services[1].close()
            old.store.reset()
            path = old.store.path
            old.close()
            replacement = TestRuntime(path, clock=clock)
            replacement.add_tokens([token])
            runtimes[1] = replacement
            services[1] = MarketService(replacement, hub=hub)
            assert not replacement.books["1"].ready
            await services[1].sync_subscriptions()
            assert replacement.books["1"].ready
            assert replacement.books["1"].ask.consumed == {}
            assert first.books["1"].ask.consumed == consumed
            assert (
                first.store.db.execute("SELECT count(*) FROM native_events").fetchone()[0] == events
            )
            assert next(iter(hub.tasks.values())) is shared and len(sockets) == 1

            # Remove the last owner without leaving an obsolete subscription.
            replacement.monitored.clear()
            first.monitored.clear()
            await hub.sync_subscriptions()
            assert not hub.tasks and not hub.active_groups

    asyncio.run(run())


@pytest.mark.parametrize("target", ["consumer", "source"])
def test_missing_book_runs_original_baseline_copy_or_disconnect_path(tmp_path, monkeypatch, target):
    async def run():
        async with complete_hub(tmp_path, monkeypatch) as state:
            hub, runtimes, _, _, _, _ = state
            plans, skips = hub.plan_runs, hub.plan_skips
            if target == "consumer":
                runtimes[1].disconnect(["1"])
            else:
                hub.runtime.books["1"].disconnect()
            await hub.sync_subscriptions()
            assert hub.plan_runs == plans + 1 and hub.plan_skips == skips
            if target == "consumer":
                assert runtimes[1].books["1"].ready
            else:
                assert not any(runtime.books["1"].ready for runtime in runtimes)
            hub.message(public_book(state[4], timestamp=1001), {"1"})
            for group in hub.streams:
                hub.refresh_stream_readiness(group)
            await hub.sync_subscriptions()
            assert all(runtime.books["1"].ready for runtime in runtimes)

    asyncio.run(run())


def test_reopened_market_never_receives_old_cached_baseline(tmp_path, monkeypatch):
    async def run():
        async with complete_hub(tmp_path, monkeypatch) as state:
            hub, runtimes, services, sockets, token, _ = state
            service, runtime = services[1], runtimes[1]
            runtime.disconnect(["1"])
            runtime.market_recovering.add("1")
            service._availability_fresh_pending.add("1")
            service._availability_subscribe_pending.add("1")
            service._availability_fresh_requested_at["1"] = asyncio.get_running_loop().time()
            service._availability_timestamp_floor["1"] = runtime.books["1"].timestamp
            plans = hub.plan_runs
            await hub.sync_subscriptions()
            assert hub.plan_runs == plans + 1 and not runtime.books["1"].ready
            assert any('"operation": "subscribe"' in message for message in sockets[0].sent)
            hub.message(public_book(token, timestamp=1001), {"1"})
            assert runtime.books["1"].ready and not runtime.market_recovering
            assert not service._availability_fresh_pending
            await hub.sync_subscriptions()
            assert hub._complete_test_plan is not None

    asyncio.run(run())


@pytest.mark.parametrize(
    "change",
    [
        "metadata",
        "tick",
        "preferences",
        "availability",
        "discovery",
        "stream",
        "task",
        "legacy",
        "unknown",
    ],
)
def test_every_uncertain_control_change_uses_original_plan(tmp_path, monkeypatch, change):
    async def run():
        async with complete_hub(tmp_path, monkeypatch) as state:
            hub, runtimes, services, _, token, clock = state
            if change == "metadata":
                runtimes[0].tokens["1"] = replace(token, event_title="different metadata")
            elif change == "tick":
                with pytest.raises(ConnectionError):
                    hub.message(
                        dict(
                            event_type="tick_size_change",
                            asset_id="1",
                            market=token.condition_id,
                            new_tick_size="0.01",
                        ),
                        {"1"},
                    )
            elif change == "preferences":
                runtimes[0].update_preferences({"orderAmount": "2"})
            elif change == "availability":
                runtimes[0].market_availability["1"] = {"status": "UNAVAILABLE"}
            elif change == "discovery":
                hub.discovery_version += 1
            elif change == "stream":
                hub.stream_state(next(iter(hub.streams)), "RECONNECTING", "ConnectionError")
            elif change == "task":
                task = next(iter(hub.tasks.values()))
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            elif change == "unknown":
                del services[0]._availability_timestamp_floor
            else:
                runtime = Runtime(tmp_path / "legacy.sqlite", clock=clock)
                runtime.add_tokens([token])
                runtimes.append(runtime)
                services.append(MarketService(runtime, hub=hub))
            plans, skips = hub.plan_runs, hub.plan_skips
            await hub.sync_subscriptions()
            assert hub.plan_runs == plans + 1 and hub.plan_skips == skips

    asyncio.run(run())


def test_any_live_consumer_always_runs_the_unchanged_parent_plan(tmp_path, monkeypatch):
    async def run():
        async with complete_hub(tmp_path, monkeypatch) as state:
            hub, _, services, _, _, _ = state
            async with controlled_runtime(tmp_path / "live.sqlite") as live:
                service = MarketService(live, hub=hub)
                services.append(service)
                parent_calls = []
                parent = PublicMarketHub._sync_subscriptions

                async def trace_parent(owner):
                    parent_calls.append(owner)
                    return await parent(owner)

                monkeypatch.setattr(PublicMarketHub, "_sync_subscriptions", trace_parent)
                skips = hub.plan_skips
                for _ in range(5):
                    await hub.sync_subscriptions()
                assert parent_calls == [hub] * 5 and hub.plan_skips == skips
                await service.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["metadata", "tick", "preferences", "recovery", "missing"])
def test_mixed_live_keeps_original_entry_and_invalidates_test_reuse(tmp_path, monkeypatch, change):
    async def run():
        async with complete_hub(tmp_path, monkeypatch, service_type=TestMarketService) as state:
            hub, runtimes, services, _, token, clock = state
            live = Runtime(
                tmp_path / "live.sqlite", mode="LIVE", clock=clock, live_factory=controlled_factory
            )
            try:
                live.add_tokens([token])
                service = create_market_service(live, hub=hub)
                assert type(service) is MarketService
                assert type(services[0]) is TestMarketService
                services.append(service)
                await service.sync_subscriptions()
                assert live.books["1"].ready
                assert hub._complete_test_plan is not None
                plans, skips = hub.plan_runs, hub.plan_skips
                for _ in range(5):
                    for test_service in services[:-1]:
                        await test_service.sync_subscriptions()
                assert hub.plan_runs == plans
                assert hub.plan_skips == skips + 10
                # LIVE and generic control requests always use the original
                # complete planner, even after a valid TEST cache hit.
                await service.sync_subscriptions()
                await hub.sync_subscriptions()
                assert hub.plan_runs == plans + 2
                assert hub._test_sync_requester is None
                plans, skips = hub.plan_runs, hub.plan_skips
                if change == "metadata":
                    live.tokens["1"] = replace(token, event_title="same ID, different metadata")
                elif change == "tick":
                    live.tokens["1"] = replace(token, tick=10_000)
                elif change == "preferences":
                    live.update_preferences({"orderAmount": "2"})
                elif change == "recovery":
                    service._availability_fresh_pending.add("1")
                else:
                    live.disconnect(["1"])
                await services[0].sync_subscriptions()
                assert hub.plan_runs == plans + 1
                assert hub.plan_skips == skips
                assert live.client._submit_order.await_count == 0
                assert live.client._cancel_order.await_count == 0
                # Close the LIVE subscription before closing its ledger.
                await service.close()
            finally:
                live.close()

    asyncio.run(run())
