"""Bounded public-page retries preserve the atomic discovery owner."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from pm_nautilus.market import GAMMA, MarketService, PublicMarketHub
from test_market import category_response
from test_market_scan_schedule import discovered_event
from test_runtime import setup


def fast_backoffs(monkeypatch):
    real_sleep = asyncio.sleep
    delays = []

    async def sleep(delay):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("pm_nautilus.market.asyncio.sleep", sleep)
    monkeypatch.setattr("pm_nautilus.market.random.uniform", lambda low, high: 0)
    return delays


@asynccontextmanager
async def service_for(tmp_path, handler):
    runtime, _, _ = setup(tmp_path)
    service = MarketService(runtime, httpx.MockTransport(handler))
    service.sync_subscriptions = AsyncMock()
    try:
        yield runtime, service
    finally:
        await service.close()
        runtime.close()


@pytest.mark.parametrize("failure", [429, 502, 503, 504, "timeout"])
def test_failed_middle_page_recovers_in_same_atomic_scan(tmp_path, monkeypatch, failure):
    async def run():
        delays = fast_backoffs(monkeypatch)
        pages = []
        params = []

        def handler(request):
            category = category_response(request)
            if category is not None:
                return category
            cursor = request.url.params.get("after_cursor")
            pages.append(cursor)
            params.append(dict(request.url.params))
            assert runtime.tokens == original and runtime.store.tokens() == stored
            assert service.discovery_status["lastSuccessAt"] is None
            assert service.discovery_status["lastAttemptAt"]
            if cursor is None:
                return httpx.Response(
                    200, json={"events": [discovered_event()], "next_cursor": "tail"}
                )
            if pages.count("tail") == 1:
                if failure == "timeout":
                    raise httpx.ReadTimeout("temporary timeout", request=request)
                return httpx.Response(failure)
            return httpx.Response(200, json={"events": []})

        async with service_for(tmp_path, handler) as (runtime, service):
            original, stored = dict(runtime.tokens), runtime.store.tokens()
            await service.scan()
            assert pages == [None, "tail", "tail"]
            assert params[1] == params[2]
            assert delays == [0.5]
            assert {"1001", "1002"} <= runtime.tokens.keys()
            assert service.scan_status["lastError"] is None
            assert service.discovery_status["lastSuccessAt"] == service.scan_status["lastScanAt"]
            assert service.discovery_status["checkedMarketCount"] == 1
            assert service.discovery_status["consecutiveFailures"] == 0
            service.sync_subscriptions.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [429, 503, "timeout"])
def test_exhausted_retry_keeps_old_scan_and_counts_one_failed_round(tmp_path, monkeypatch, failure):
    async def run():
        delays = fast_backoffs(monkeypatch)
        pages = []
        failing = True

        def handler(request):
            category = category_response(request)
            if category is not None:
                return category
            cursor = request.url.params.get("after_cursor")
            pages.append(cursor)
            if cursor is None:
                return httpx.Response(
                    200, json={"events": [discovered_event()], "next_cursor": "tail"}
                )
            if failing:
                if failure == "timeout":
                    raise httpx.ReadTimeout("temporary timeout", request=request)
                return httpx.Response(failure)
            return httpx.Response(200, json={"events": []})

        async with service_for(tmp_path, handler) as (runtime, service):
            original, stored = dict(runtime.tokens), runtime.store.tokens()
            service.scan_status["lastScanAt"] = "previous-success"
            service.discovery_status["lastSuccessAt"] = "previous-success"
            assert service.discovery_status["monitoringSince"] is None
            await service.scan()
            assert pages == [None, "tail", "tail", "tail"]
            assert delays == [0.5, 1]
            assert runtime.tokens == original and runtime.store.tokens() == stored
            assert service.scan_status["lastScanAt"] == "previous-success"
            assert service.discovery_status["lastSuccessAt"] == "previous-success"
            assert service.discovery_status["consecutiveFailures"] == 1
            assert service.discovery_status["error"] == (
                "ReadTimeout" if failure == "timeout" else "HTTPStatusError"
            )
            first_failure = service.discovery_status["failureSince"]
            assert first_failure == service.discovery_status["lastAttemptAt"]
            assert first_failure == service.discovery_status["monitoringSince"]
            service.sync_subscriptions.assert_not_awaited()
            await service.scan()
            assert service.discovery_status["consecutiveFailures"] == 2
            assert service.discovery_status["failureSince"] == first_failure
            failing = False
            await service.scan()
            assert service.discovery_status["consecutiveFailures"] == 0
            assert service.discovery_status["error"] is None
            assert service.discovery_status["failureSince"] is None
            assert service.discovery_status["lastSuccessAt"] != "previous-success"

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", [400, 401, 403, 404, 500, "connection", "json", "payload", "page"]
)
def test_nontransient_and_invalid_payloads_are_not_retried(tmp_path, monkeypatch, failure):
    async def run():
        delays = fast_backoffs(monkeypatch)
        calls = 0

        def handler(request):
            nonlocal calls
            category = category_response(request)
            if category is not None:
                return category
            calls += 1
            if failure == "connection":
                raise httpx.ConnectError("connection failed", request=request)
            if failure == "json":
                return httpx.Response(200, content="not json")
            if failure == "payload":
                return httpx.Response(200, json=[])
            if failure == "page":
                return httpx.Response(200, json={"events": {}})
            return httpx.Response(failure)

        async with service_for(tmp_path, handler) as (runtime, service):
            original = dict(runtime.tokens)
            await service.scan()
            assert calls == 1 and not delays
            assert runtime.tokens == original
            assert service.scan_status["lastError"]
            assert service.discovery_status["consecutiveFailures"] == 1
            assert service.discovery_status["lastSuccessAt"] is None

    asyncio.run(run())


@pytest.mark.parametrize("value,expected", [("2", 2), ("600", 5), ("-5", 0.5), ("bad", 0.5)])
def test_retry_after_seconds_is_bounded(tmp_path, monkeypatch, value, expected):
    async def run():
        delays = fast_backoffs(monkeypatch)
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"Retry-After": value})
            return httpx.Response(200, json={"events": []})

        async with service_for(tmp_path, handler) as (_, service):
            assert await service.discovery_page() == {"events": []}
            assert delays == [expected] and calls == 2

    asyncio.run(run())


def test_retry_after_http_date_and_jitter_are_bounded(tmp_path, monkeypatch):
    async def run():
        delays = fast_backoffs(monkeypatch)
        monkeypatch.setattr("pm_nautilus.market.random.uniform", lambda low, high: high)
        calls = 0
        future = format_datetime(datetime.now(timezone.utc) + timedelta(minutes=10), usegmt=True)

        def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503, headers={"Retry-After": future})
            return httpx.Response(200, json={"events": []})

        async with service_for(tmp_path, handler) as (_, service):
            await service.discovery_page()
            assert delays == [5.25] and calls == 2

    asyncio.run(run())


def test_combined_page_deadline_bounds_retry_requests_and_preserves_old_state(
    tmp_path, monkeypatch
):
    async def run():
        fast_backoffs(monkeypatch)
        monkeypatch.setattr("pm_nautilus.market.DISCOVERY_PAGE_SECONDS", 0.02)
        calls = 0

        async def handler(request):
            nonlocal calls
            category = category_response(request)
            if category is not None:
                return category
            calls += 1
            if calls == 1:
                return httpx.Response(503)
            await asyncio.Event().wait()

        async with service_for(tmp_path, handler) as (runtime, service):
            original = dict(runtime.tokens)
            await asyncio.wait_for(service.scan(), 1)
            # A backoff that cannot fit aborts without starting another request.
            assert calls == 1
            assert runtime.tokens == original
            assert service.discovery_status["error"] == "HTTPStatusError"
            assert service.discovery_status["lastSuccessAt"] is None

    asyncio.run(run())


def test_page_deadline_cancels_stuck_request_without_retry(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr("pm_nautilus.market.DISCOVERY_PAGE_SECONDS", 0.01)
        calls = 0
        cancelled = asyncio.Event()

        async def handler(request):
            nonlocal calls
            calls += 1
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async with service_for(tmp_path, handler) as (_, service):
            with pytest.raises(httpx.ReadTimeout, match="重试时间用尽"):
                await asyncio.wait_for(service.discovery_page(), 1)
            assert calls == 1 and cancelled.is_set()

    asyncio.run(run())


def test_close_during_backoff_never_retries_or_commits_late(tmp_path, monkeypatch):
    async def run():
        entered = asyncio.Event()
        calls = 0

        async def sleep(delay):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr("pm_nautilus.market.asyncio.sleep", sleep)

        def handler(request):
            nonlocal calls
            category = category_response(request)
            if category is not None:
                return category
            calls += 1
            return httpx.Response(503)

        async with service_for(tmp_path, handler) as (runtime, service):
            original, stored = dict(runtime.tokens), runtime.store.tokens()
            service.scan_task = asyncio.create_task(service.run_scan())
            await asyncio.wait_for(entered.wait(), 1)
            await service.close()
            assert service.scan_task.cancelled() and calls == 1
            assert runtime.tokens == original and runtime.store.tokens() == stored
            assert service.discovery_status["enabled"] is False
            assert service.discovery_status["lastSuccessAt"] is None
            assert service.discovery_status["consecutiveFailures"] == 0

    asyncio.run(run())


def test_generation_change_during_retry_does_not_commit_discovery(tmp_path, monkeypatch):
    async def run():
        fast_backoffs(monkeypatch)
        calls = 0

        def handler(request):
            nonlocal calls
            category = category_response(request)
            if category is not None:
                return category
            calls += 1
            if calls == 1:
                runtime.store.put("generation", "new-generation")
                return httpx.Response(503)
            return httpx.Response(200, json={"events": [discovered_event()]})

        async with service_for(tmp_path, handler) as (runtime, service):
            original, stored = dict(runtime.tokens), runtime.store.tokens()
            await service.scan()
            assert runtime.tokens == original and runtime.store.tokens() == stored
            assert service.discovery_status["lastSuccessAt"] is None
            service.sync_subscriptions.assert_not_awaited()

    asyncio.run(run())


def test_non_discovery_get_does_not_retry(tmp_path, monkeypatch):
    async def run():
        delays = fast_backoffs(monkeypatch)
        paths = []

        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(503)

        async with service_for(tmp_path, handler) as (_, service):
            with pytest.raises(httpx.HTTPStatusError):
                await service.get(f"{GAMMA}/markets/123")
            assert paths == ["/markets/123"] and not delays

    asyncio.run(run())


def test_shared_consumers_use_one_retrying_scan_and_share_diagnostics(tmp_path, monkeypatch):
    async def run():
        fast_backoffs(monkeypatch)
        calls = 0

        def handler(request):
            nonlocal calls
            category = category_response(request)
            if category is not None:
                return category
            calls += 1
            if calls == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"events": [discovered_event()]})

        first, _, _ = setup(tmp_path / "first")
        second, _, _ = setup(tmp_path / "second")
        hub = PublicMarketHub(httpx.MockTransport(handler))
        hub.sync_subscriptions = AsyncMock()
        one, two = MarketService(first, hub=hub), MarketService(second, hub=hub)
        try:
            await asyncio.gather(one.scan(), two.scan())
            assert calls == 2 and hub.discovery_version == 1
            assert one.discovery_status == two.discovery_status == hub.discovery_status
            assert one.discovery_status is not hub.discovery_status
            assert {"1001", "1002"} <= first.tokens.keys()
            assert {"1001", "1002"} <= second.tokens.keys()
        finally:
            await one.close()
            await two.close()
            await hub.close()
            first.close()
            second.close()

    asyncio.run(run())
