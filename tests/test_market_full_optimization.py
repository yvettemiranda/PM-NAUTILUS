"""Pending close, unchanged consumer and independent control-loop boundaries."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from pm_nautilus.market import MarketService, PublicMarketHub
from pm_nautilus.strategy import Runtime
from test_live_recovery import controlled_runtime, token as live_token
from test_market_efficiency import IdleSocket, public_book, settle_tasks
from test_runtime import setup


def test_moved_books_are_unready_before_waiting_for_old_socket_close(tmp_path, monkeypatch):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class PendingClose(IdleSocket):
            block = False

            async def __aexit__(self, *args):
                if self.block:
                    entered.set()
                    await release.wait()
                return False

        monkeypatch.setattr("pm_nautilus.market.connect", lambda *a, **k: PendingClose())
        runtime, _, t = setup(tmp_path)
        runtime.add_tokens([replace(t, token_id=str(n), event_id=f"e{n}") for n in range(1, 401)])
        hub = PublicMarketHub()
        service = MarketService(runtime, hub=hub)
        migration = None
        try:
            await service.sync_subscriptions()
            await settle_tasks()
            groups = sorted(hub.active_groups)
            selected = {min(hub.active_groups[group]) for group in groups}
            source = groups[-1]
            moved = min(hub.active_groups[source])
            hub.sockets[source].block = True
            for tid in selected:
                hub.message(public_book(runtime.tokens[tid]), {tid})
            runtime.monitored = selected
            runtime.store.put("status", "RUNNING")
            migration = asyncio.create_task(service.sync_subscriptions())
            await asyncio.wait_for(entered.wait(), 1)
            assert source not in hub.active_groups
            assert not hub.runtime.books[moved].ready and not runtime.books[moved].ready
            runtime.evaluate(runtime.tokens[moved].event_id)
            assert not runtime.store.intents()
            hub.message(
                public_book(runtime.tokens[moved], timestamp=2000), hub.subscription_ids(source)
            )
            assert not runtime.books[moved].ready
            release.set()
            await migration
            hub.message(
                public_book(runtime.tokens[moved], timestamp=2001), hub.subscription_ids(groups[0])
            )
            assert runtime.books[moved].ready
            assert (
                runtime.business["cycles"][runtime.tokens[moved].event_id]["quantity"] == 50_000_000
            )
        finally:
            release.set()
            if migration is not None:
                await asyncio.gather(migration, return_exceptions=True)
            await service.close()
            await hub.close()
            runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["close", "filter"])
def test_test_subscription_reduction_preserves_scattered_live_books(tmp_path, monkeypatch, change):
    async def run():
        monkeypatch.setattr("pm_nautilus.market.connect", lambda *a, **k: IdleSocket())
        async with controlled_runtime(tmp_path / "live.sqlite") as live:
            t = live_token(live)
            test = Runtime(tmp_path / "test.sqlite")
            test.add_tokens([replace(t, token_id=str(n)) for n in range(1, 401)])
            hub = PublicMarketHub()
            first, second = MarketService(test, hub=hub), MarketService(live, hub=hub)
            try:
                await first.sync_subscriptions()
                await settle_tasks()
                selected = {min(ids) for ids in hub.active_groups.values()}
                live.add_tokens([test.tokens[tid] for tid in selected])
                await second.sync_subscriptions()
                for tid in selected:
                    hub.message(public_book(live.tokens[tid]), {tid})
                tasks = dict(hub.tasks)
                if change == "close":
                    await first.close()
                else:
                    test.monitored = selected
                    await first.sync_subscriptions()
                assert hub.tasks == tasks and all(not task.done() for task in tasks.values())
                assert live.monitored == selected
                assert all(live.books[tid].ready for tid in selected)
                assert not live.store.intents()
            finally:
                await first.close()
                await second.close()
                await hub.close()
                test.close()

    asyncio.run(run())


def test_resolution_poll_does_not_delay_control_ticks_and_is_joined_on_close(tmp_path, monkeypatch):
    async def run():
        runtime, _, _ = setup(tmp_path)
        service = MarketService(runtime)
        service.scan = AsyncMock()
        service.sync_subscriptions = AsyncMock()
        entered, finished = asyncio.Event(), asyncio.Event()
        real_sleep = asyncio.sleep

        async def fast_control_sleep(delay):
            await real_sleep(0.005 if delay == 5 else delay)

        async def slow_resolution():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        monkeypatch.setattr("pm_nautilus.market.asyncio.sleep", fast_control_sleep)
        refresh = Mock(wraps=runtime.refresh_eligibility)
        monkeypatch.setattr(runtime, "refresh_eligibility", refresh)
        service.on_resolution = slow_resolution
        service.loop_task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await real_sleep(0.03)
            assert refresh.call_count >= 3
            resolution = service.resolution_task
            assert resolution is not None and not resolution.done()
            await service.close()
            assert resolution.cancelled() and finished.is_set()
            assert service.loop_task.done()
        finally:
            await service.close()
            runtime.close()

    asyncio.run(run())
