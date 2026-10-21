"""Discovery waits must not own control ticks or outlive their consumer."""

import asyncio
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from pm_nautilus.market import MarketService, PublicMarketHub
from test_market import category_response, event
from test_market_efficiency import IdleSocket, public_book, settle_tasks
from test_runtime import setup


def control_waiter(monkeypatch):
    real_sleep = asyncio.sleep

    async def accelerated_sleep(delay):
        await real_sleep(0.001 if delay >= 0.5 else delay)

    async def until(predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await real_sleep(0.001)

    monkeypatch.setattr("pm_nautilus.market.asyncio.sleep", accelerated_sleep)
    return until


def discovered_event():
    raw = event(123)
    raw["markets"][0]["clobTokenIds"] = ["1001", "1002"]
    return raw


def test_slow_scan_keeps_control_resolution_and_metadata_atomic(tmp_path, monkeypatch):
    async def run():
        until = control_waiter(monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        pages = []

        async def handler(request):
            category = category_response(request)
            if category is not None:
                return category
            pages.append(request.url.params.get("after_cursor"))
            if pages[-1] is None:
                return httpx.Response(
                    200, json={"events": [discovered_event()], "next_cursor": "last"}
                )
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"events": []})

        runtime, clock, token = setup(tmp_path)
        service = MarketService(runtime, httpx.MockTransport(handler))
        service.sync_subscriptions = AsyncMock()
        service.on_resolution = AsyncMock()
        refresh = Mock(wraps=runtime.refresh_eligibility)
        monkeypatch.setattr(runtime, "refresh_eligibility", refresh)
        original = dict(runtime.tokens)
        stored = runtime.store.tokens()
        service.loop_task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task = service.scan_task
            count = refresh.call_count
            clock.set_time(token.ends_ns)
            await until(lambda: refresh.call_count >= count + 3)
            assert token.token_id not in runtime.monitored
            assert service.on_resolution.await_count >= 2
            assert service.scan_task is task and not task.done()
            assert pages == [None, "last"]
            assert runtime.tokens == original and runtime.store.tokens() == stored
            assert service.scan_status["scanning"] is True
            service.sync_subscriptions.assert_not_awaited()
            release.set()
            await until(lambda: service._scan_finished_at is not None)
            assert {"1001", "1002"} <= runtime.tokens.keys()
            assert service.scan_status["lastScanAt"]
            assert service.scan_status["lastError"] is None
            assert service.sync_subscriptions.await_count
            assert not runtime.store.intents()
        finally:
            release.set()
            await service.close()
            runtime.close()

    asyncio.run(run())


def test_failed_middle_page_keeps_old_discovery_and_does_not_busy_retry(tmp_path, monkeypatch):
    async def run():
        until = control_waiter(monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        pages = []

        async def handler(request):
            category = category_response(request)
            if category is not None:
                return category
            pages.append(request.url.params.get("after_cursor"))
            if pages[-1] is None:
                return httpx.Response(
                    200, json={"events": [discovered_event()], "next_cursor": "failed"}
                )
            entered.set()
            await release.wait()
            return httpx.Response(503)

        runtime, _, _ = setup(tmp_path)
        runtime.start()
        service = MarketService(runtime, httpx.MockTransport(handler))
        service.scan_status["lastScanAt"] = "previous-success"
        service.sync_subscriptions = AsyncMock()
        original = dict(runtime.tokens)
        stored = runtime.store.tokens()
        service.loop_task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            release.set()
            await until(lambda: service._scan_finished_at is not None)
            await until(lambda: service.sync_subscriptions.await_count >= 3)
            assert pages == [None, "failed", "failed", "failed"]
            assert runtime.tokens == original and runtime.store.tokens() == stored
            assert service.scan_status["lastScanAt"] == "previous-success"
            assert "HTTPStatusError" in service.scan_status["lastError"]
            assert service.scan_status["scanning"] is False
            assert service.scan_status.get("serviceError") is None
            assert runtime.status == "RUNNING"
        finally:
            release.set()
            await service.close()
            runtime.close()

    asyncio.run(run())


def test_scan_schedule_has_one_owner_and_waits_from_completion(tmp_path, monkeypatch):
    async def run():
        until = control_waiter(monkeypatch)
        runtime, _, _ = setup(tmp_path)
        service = MarketService(runtime)
        release = asyncio.Event()
        active = maximum = calls = 0

        async def scan():
            nonlocal active, maximum, calls
            calls += 1
            active += 1
            maximum = max(maximum, active)
            try:
                await release.wait()
            finally:
                active -= 1

        service.scan = scan
        service.sync_subscriptions = AsyncMock()
        refresh = Mock(wraps=runtime.refresh_eligibility)
        monkeypatch.setattr(runtime, "refresh_eligibility", refresh)
        service.loop_task = asyncio.create_task(service.run())
        try:
            await until(lambda: calls == 1)
            count = refresh.call_count
            await until(lambda: refresh.call_count >= count + 4)
            assert calls == 1 and maximum == 1
            release.set()
            await until(lambda: service._scan_finished_at is not None)
            await until(lambda: service.scan_task is None)
            release.clear()
            count = refresh.call_count
            await until(lambda: refresh.call_count >= count + 4)
            assert calls == 1
            service._scan_finished_at -= 301
            await until(lambda: calls == 2)
            task = service.scan_task
            count = refresh.call_count
            await until(lambda: refresh.call_count >= count + 4)
            assert service.scan_task is task and maximum == 1 and calls == 2
            await service.close()
            assert task.cancelled() and active == 0 and service.loop_task.done()
        finally:
            release.set()
            await service.close()
            runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("error", [RuntimeError("discovery failed"), TypeError("invalid scanner")])
def test_unexpected_scan_failure_pauses_and_remains_observable(tmp_path, monkeypatch, error):
    async def run():
        until = control_waiter(monkeypatch)
        runtime, _, _ = setup(tmp_path)
        runtime.start()
        service = MarketService(runtime)
        service.scan = AsyncMock(side_effect=error)
        service.sync_subscriptions = AsyncMock()
        service.loop_task = asyncio.create_task(service.run())
        try:
            await until(lambda: service.scan_task is None and service._scan_finished_at is not None)
            await until(lambda: service.sync_subscriptions.await_count >= 3)
            assert runtime.status == "PAUSED"
            assert service.scan_status["serviceError"] == type(error).__name__
            assert str(error) in service.scan_status["lastServiceError"]
            assert runtime.store.get("scan")["lastServiceErrorAt"]
            service.scan.assert_awaited_once()
            assert not service.loop_task.done()
        finally:
            await service.close()
            runtime.close()

    asyncio.run(run())


def test_close_waits_for_cancelled_scan_and_rejects_late_page(tmp_path, monkeypatch):
    async def run():
        control_waiter(monkeypatch)
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(request):
            category = category_response(request)
            if category is not None:
                return category
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                # A transport's cancellation cleanup can finish with a response.
                # The closed consumer must not apply it to the old generation.
                await release.wait()
            return httpx.Response(200, json={"events": [discovered_event()]})

        runtime, _, _ = setup(tmp_path)
        service = MarketService(runtime, httpx.MockTransport(handler))
        service.sync_subscriptions = AsyncMock()
        original = dict(runtime.tokens)
        stored = runtime.store.tokens()
        service.loop_task = asyncio.create_task(service.run())
        closing = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task = service.scan_task
            closing = asyncio.create_task(service.close())
            await asyncio.wait_for(cancelled.wait(), 3)
            assert service.closed and not closing.done() and not task.done()
            assert not service.http.is_closed
            release.set()
            await closing
            assert task.done() and service.loop_task.done() and service.http.is_closed
            assert runtime.tokens == original and runtime.store.tokens() == stored
            service.sync_subscriptions.assert_not_awaited()
            runtime.close()
            await settle_tasks()
            assert task.done()
        finally:
            release.set()
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            await service.close()
            if not runtime.store._closed:
                runtime.close()

    asyncio.run(run())


def test_closing_shared_scan_waiter_leaves_source_and_other_consumer_live(tmp_path, monkeypatch):
    async def run():
        until = control_waiter(monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        pages = []

        async def handler(request):
            category = category_response(request)
            if category is not None:
                return category
            pages.append(request.url.path)
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"events": [discovered_event()]})

        monkeypatch.setattr("pm_nautilus.market.connect", lambda *a, **k: IdleSocket())
        first, _, token = setup(tmp_path / "first")
        second, _, _ = setup(tmp_path / "second")
        hub = PublicMarketHub(httpx.MockTransport(handler))
        owner, waiter = MarketService(first, hub=hub), MarketService(second, hub=hub)
        try:
            await owner.sync_subscriptions()
            await settle_tasks()
            hub.message(public_book(token), {token.token_id})
            sockets = dict(hub.tasks)
            assert first.books[token.token_id].ready
            owner.loop_task = asyncio.create_task(owner.run())
            await asyncio.wait_for(entered.wait(), 3)
            waiter.loop_task = asyncio.create_task(waiter.run())
            await until(lambda: waiter.scan_task is not None)
            await settle_tasks()
            assert not waiter.scan_task.done() and pages == ["/events/keyset"]
            apply_waiter = Mock(wraps=second.add_tokens)
            monkeypatch.setattr(second, "add_tokens", apply_waiter)
            task = owner.scan_task
            await waiter.close()
            assert waiter.scan_task.cancelled()
            second.close()
            assert owner.scan_task is task and not task.done()
            assert first.books[token.token_id].ready and hub.tasks == sockets
            release.set()
            await until(lambda: owner._scan_finished_at is not None)
            assert pages == ["/events/keyset"]
            assert {"1001", "1002"} <= first.tokens.keys()
            apply_waiter.assert_not_called()
            assert waiter.closed and waiter.loop_task.cancelled()
        finally:
            release.set()
            await owner.close()
            await waiter.close()
            await hub.close()
            first.close()
            if not second.store._closed:
                second.close()

    asyncio.run(run())
