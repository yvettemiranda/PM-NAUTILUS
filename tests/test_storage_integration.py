"""Controlled capacity transitions at real START, BUY, exit, and discovery boundaries."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from nautilus_trader.model.identifiers import VenueOrderId

from pm_nautilus.app import create_app
from pm_nautilus.live import LiveExecution
from pm_nautilus.market import MarketService, PublicMarketHub, normalize
from pm_nautilus.performance import PerformanceSampler
from pm_nautilus.storage_guard import GIB, StorageGuard

from test_live_order_terms import prepared_sell, unsigned_sdk
from test_live_recovery import accept, controlled_runtime, flush, venue_trade
from test_live_submit_cleanup import prepared_order
from test_market import category_response, event
from test_runtime import setup


class ControlledStorage:
    def __init__(self, path):
        self.capacity = SimpleNamespace(
            f_frsize=1,
            f_bsize=4096,
            f_blocks=20 * GIB,
            f_bavail=10 * GIB,
            f_bfree=20 * GIB,
            f_files=100_000,
            f_favail=100_000,
        )
        self.guard = StorageGuard(path, statvfs=lambda _: self.capacity)

    async def set(self, free):
        self.capacity.f_bavail = free
        await self.guard.refresh()


def test_direct_buy_pressure_has_no_checkpoint_intent_or_native_side_effects(tmp_path, monkeypatch):
    runtime, _, token = setup(tmp_path)
    storage = ControlledStorage(tmp_path)
    runtime.storage_guard = storage.guard
    asyncio.run(storage.set(GIB))
    before_changes = runtime.store.db.total_changes
    before_cash = runtime.cash()
    try:
        for owner, name in (
            (runtime.native, "ensure_instrument"),
            (runtime, "checkpoint_book"),
            (runtime.store, "save_intent"),
            (runtime.store, "journal"),
        ):
            monkeypatch.setattr(owner, name, Mock(side_effect=AssertionError(name)))
        assert runtime.submit(token, "BUY", 10_000_000, 100_000, cash=1_000_000) is None
        assert runtime.store.db.total_changes == before_changes
        assert not runtime.store.intents()
        assert not runtime.store.events()
        assert not runtime.native.cache.orders()
        assert runtime.cash() == before_cash
        assert runtime.status == "PAUSED"
    finally:
        runtime.close()


def test_start_rechecks_pressure_before_any_validation_or_running_state(tmp_path, monkeypatch):
    runtime, _, _ = setup(tmp_path)
    storage = ControlledStorage(tmp_path)
    runtime.storage_guard = storage.guard

    async def run():
        await storage.set(10 * GIB)
        assert runtime.storage_can_open
        # Capacity changes after the last background sample; START must probe.
        storage.capacity.f_bavail = GIB
        validation = AsyncMock(side_effect=AssertionError("unsafe START validation"))
        monkeypatch.setattr(runtime, "validate_async", validation)
        before = runtime.store.db.total_changes
        with pytest.raises(ValueError, match="磁盘"):
            await runtime.start_async()
        validation.assert_not_awaited()
        assert runtime.status == "PAUSED"
        assert runtime.store.db.total_changes == before
        assert not runtime.store.intents()

    try:
        asyncio.run(run())
    finally:
        runtime.close()


def test_pressure_between_async_start_validation_and_finish_cannot_enter_running(
    tmp_path, monkeypatch
):
    runtime, _, _ = setup(tmp_path)
    runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    storage = ControlledStorage(tmp_path)
    runtime.storage_guard = storage.guard
    validate = runtime.validate_async

    async def validate_then_pressure():
        result = await validate()
        assert result["ok"]
        await storage.set(GIB)
        return result

    monkeypatch.setattr(runtime, "validate_async", validate_then_pressure)

    async def run():
        await storage.set(10 * GIB)
        with pytest.raises(ValueError, match="磁盘"):
            await runtime.start_async()
        assert runtime.status == "PAUSED"
        assert not runtime.store.intents()
        assert not runtime.native.cache.orders()
        assert runtime.cash() == 100_000_000

    try:
        asyncio.run(run())
    finally:
        runtime.close()


def test_running_pressure_suppresses_new_buys_without_pausing_or_auto_starting(tmp_path):
    runtime, _, _ = setup(tmp_path)
    storage = ControlledStorage(tmp_path)
    runtime.storage_guard = storage.guard

    async def run():
        await storage.set(10 * GIB)
        await runtime.start_async()  # Incomplete book cannot buy yet.
        assert runtime.status == "RUNNING"
        await storage.set(GIB)
        runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        runtime.evaluate("event")
        assert runtime.evaluations["event"][0] == "READY"
        assert runtime.status == "RUNNING"
        assert not runtime.store.intents()
        assert not runtime.native.cache.orders()
        runtime.pause()
        await storage.set(4 * GIB)
        assert storage.guard.can_open
        assert runtime.status == "PAUSED"
        assert not runtime.store.intents()

    try:
        asyncio.run(run())
    finally:
        runtime.close()


def test_quiet_ready_event_retries_once_after_storage_recovers(tmp_path):
    runtime, _, _ = setup(tmp_path)
    storage = ControlledStorage(tmp_path)
    runtime.storage_guard = storage.guard

    async def run():
        await storage.set(10 * GIB)
        await runtime.start_async()
        await storage.set(GIB)
        runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        runtime.refresh_eligibility()
        runtime.drain()
        assert not runtime.store.intents() and not runtime.dirty
        await storage.set(4 * GIB)
        runtime.refresh_eligibility()
        runtime.drain()
        assert len(runtime.native.cache.orders()) == 1
        assert runtime.status == "RUNNING"
        runtime.refresh_eligibility()
        runtime.drain()
        assert len(runtime.native.cache.orders()) == 1
        assert runtime.validate()["ok"]

    try:
        asyncio.run(run())
    finally:
        runtime.close()


def test_existing_target_exit_continues_under_pressure_with_exact_ownership(tmp_path):
    runtime, _, _ = setup(tmp_path)
    storage = ControlledStorage(tmp_path)
    runtime.storage_guard = storage.guard

    async def run():
        await storage.set(10 * GIB)
        runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        await runtime.start_async()
        assert runtime.business["cycles"]["event"]["quantity"] == 50_000_000
        await storage.set(GIB)
        runtime.book("1", [(35_000, 5_000_000)], [(20_000, 50_000_000)])
        assert runtime.business["cycles"]["event"]["quantity"] == 45_000_000
        assert runtime.cash() == 99_175_000
        assert runtime.status == "RUNNING"
        assert [intent["side"] for intent in runtime.store.intents().values()] == ["BUY", "SELL"]
        assert runtime.validate()["ok"]

    try:
        asyncio.run(run())
    finally:
        runtime.close()


def test_warning_still_allows_explicit_start_and_normal_buy(tmp_path):
    runtime, _, _ = setup(tmp_path)
    storage = ControlledStorage(tmp_path)
    runtime.storage_guard = storage.guard

    async def run():
        await storage.set(3 * GIB)
        assert storage.guard.status == "WARNING"
        runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        await runtime.start_async()
        assert runtime.status == "RUNNING"
        assert runtime.business["cycles"]["event"]["quantity"] == 50_000_000
        assert runtime.validate()["ok"]

    try:
        asyncio.run(run())
    finally:
        runtime.close()


def test_live_buy_post_guard_rechecks_pressure_after_native_order_exists(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "live-buy.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            storage = ControlledStorage(tmp_path)
            runtime.storage_guard = storage.guard
            await storage.set(10 * GIB)
            runtime.client.buy_valid = LiveExecution.buy_valid.__get__(runtime.client)
            assert runtime.client.buy_valid(order, runtime.store.intent(oid))
            await storage.set(GIB)
            assert not runtime.client.buy_valid(order, runtime.store.intent(oid))
            before = deepcopy(runtime.store.intent(oid))
            before_events = len(runtime.store.events())
            with pytest.raises(ValueError, match="磁盘"):
                runtime.client._check_post_ready(oid)
            assert runtime.store.intent(oid) == before
            assert len(runtime.store.events()) == before_events
            assert runtime.held_cash() == 1_000_000
            assert runtime.status == "RUNNING"

    asyncio.run(run())


def test_live_sell_post_guard_keeps_valid_exit_available_under_pressure(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "live-sell.sqlite") as runtime:
            oid, order = await prepared_sell(runtime)
            client = runtime.client
            token = runtime.tokens["1"]
            client._http_client = unsigned_sdk(token)
            client._sdk_terms.clear()
            client._post_signed_order = AsyncMock()
            client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-sell"))
            await client._submit_limit_order(SimpleNamespace(order=order), None)
            client._check_post_ready(oid)
            storage = ControlledStorage(tmp_path)
            runtime.storage_guard = storage.guard
            await storage.set(GIB)
            client.buy_valid = Mock(side_effect=AssertionError("SELL must not use BUY guard"))
            before = deepcopy(runtime.store.intent(oid))
            client._check_post_ready(oid)
            assert runtime.store.intent(oid) == before
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_late_confirmed_live_buy_is_projected_once_under_pressure(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "live-confirmed.sqlite") as runtime:
            oid, _ = await prepared_order(runtime)
            storage = ControlledStorage(tmp_path)
            runtime.storage_guard = storage.guard
            await storage.set(GIB)
            await accept(runtime, oid)
            trade = venue_trade(runtime.tokens["1"])
            runtime.client.ingest_trade(trade)
            await flush()
            assert runtime.business["cycles"]["e1"]["quantity"] == 10_000_000
            # The confirmed partial fill reduces, but cannot erase, the unknown
            # remainder's reservation merely because disk protection is active.
            assert runtime.held_cash() == 1_000_000
            fills = len(runtime.store.events())
            runtime.client.ingest_trade(trade)
            await flush()
            assert len(runtime.store.events()) == fills
            assert runtime.business["cycles"]["e1"]["quantity"] == 10_000_000
            assert runtime.validate()["ok"]
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_critical_scan_makes_no_requests_or_writes_and_preserves_running_state(tmp_path):
    runtime, _, original = setup(tmp_path)
    runtime.start()
    storage = ControlledStorage(tmp_path)
    runtime.storage_guard = storage.guard
    runtime.store.put("scan", {"lastScanAt": "last-complete", "lastError": None})
    requests = []

    def handler(request):
        requests.append(request.url.path)
        raise AssertionError("Critical scan must not fetch metadata")

    async def run():
        service = MarketService(runtime, httpx.MockTransport(handler))
        try:
            await storage.set(GIB)
            before = runtime.store.db.total_changes
            await service.scan()
            assert not requests
            assert runtime.store.db.total_changes == before
            assert runtime.tokens["1"] == original
            assert runtime.status == "RUNNING"
            assert service.scan_status["lastScanAt"] == "last-complete"
            assert not service.scan_status.get("serviceError")
            assert not service.scan_status.get("lastError")
        finally:
            await service.close()

    try:
        asyncio.run(run())
    finally:
        runtime.close()


def test_scan_pressure_after_awaited_page_never_replaces_old_metadata(tmp_path):
    runtime, _, original = setup(tmp_path)
    runtime.start()
    storage = ControlledStorage(tmp_path)
    runtime.storage_guard = storage.guard
    runtime.store.put("scan", {"lastScanAt": "last-complete", "lastError": None})
    changed = event(99)
    changed["markets"][0]["clobTokenIds"] = ["99", "100"]

    async def handler(request):
        assert request.url.path == "/events/keyset"
        await storage.set(GIB)
        return httpx.Response(200, json={"events": [changed], "next_cursor": None})

    async def run():
        await storage.set(10 * GIB)
        service = MarketService(runtime, httpx.MockTransport(handler))
        service.categories_refresh = AsyncMock()
        service.sync_subscriptions = AsyncMock()
        try:
            before = runtime.store.db.total_changes
            await service.scan()
            assert runtime.store.db.total_changes == before
            assert runtime.tokens == {"1": original}
            assert runtime.store.tokens() == {"1": original}
            assert service.scan_status["lastScanAt"] == "last-complete"
            assert not service.scan_status.get("serviceError")
            assert not service.scan_status.get("lastError")
            assert runtime.status == "RUNNING"
            service.sync_subscriptions.assert_not_awaited()
        finally:
            await service.close()

    try:
        asyncio.run(run())
    finally:
        runtime.close()


def test_category_pressure_after_await_does_not_persist_partial_category_refresh(tmp_path):
    runtime, _, original = setup(tmp_path)
    storage = ControlledStorage(tmp_path)
    runtime.storage_guard = storage.guard
    runtime.store.put("categories", [{"id": "old", "label": "Old"}])
    requests = []

    async def handler(request):
        requests.append(request.url.path)
        response = category_response(request)
        assert response is not None
        await storage.set(GIB)
        return response

    async def run():
        await storage.set(10 * GIB)
        service = MarketService(runtime, httpx.MockTransport(handler))
        try:
            before = runtime.store.db.total_changes
            await service.scan()
            assert len(requests) == 1
            assert runtime.store.db.total_changes == before
            assert runtime.store.get("categories") == [{"id": "old", "label": "Old"}]
            assert service.categories == [{"id": "old", "label": "Old"}]
            assert runtime.tokens == {"1": original}
            assert not service.scan_status.get("serviceError")
        finally:
            await service.close()

    try:
        asyncio.run(run())
    finally:
        runtime.close()


def test_shared_hub_pressure_consumer_cannot_block_other_consumer_or_lose_rights(tmp_path):
    one_path, two_path = tmp_path / "one", tmp_path / "two"
    one_path.mkdir()
    two_path.mkdir()
    one, _, old = setup(one_path)
    two, _, _ = setup(two_path)
    storage = ControlledStorage(one_path)
    one.storage_guard = storage.guard
    one.start = Mock(side_effect=AssertionError("No implicit start"))
    one.store.put("scan", {"lastScanAt": "one-complete", "lastError": None})
    changed = replace(normalize(event(99))[0], token_id="99")

    async def run():
        hub = PublicMarketHub()
        first, second = MarketService(one, hub=hub), MarketService(two, hub=hub)
        try:
            await storage.set(GIB)
            hub.runtime.tokens = {changed.token_id: changed}
            hub.categories = [{"id": "21", "label": "Crypto"}]
            hub.discovery_version = 1
            hub.scan_status.update(lastScanAt="new-complete", lastError=None)
            before = one.store.db.total_changes
            hub.apply_discovery(first)
            hub.apply_discovery(second)
            assert one.store.db.total_changes == before
            assert one.tokens == {"1": old}
            assert first.scan_status["lastScanAt"] == "one-complete"
            assert not first.scan_status.get("serviceError")
            assert hub.consumer_versions.get(first) is None
            assert two.tokens["99"] == changed
            assert not two.tokens["1"].open
            assert hub.consumer_versions[second] == 1
            await storage.set(4 * GIB)
            hub.apply_discovery(first)
            assert one.tokens["99"] == changed
            assert hub.consumer_versions[first] == 1
            assert one.status == "PAUSED"
        finally:
            await first.close()
            await second.close()
            await hub.close()

    try:
        asyncio.run(run())
    finally:
        one.close()
        two.close()


def test_analytics_sampling_skips_pressure_and_resumes_without_deleting_history(tmp_path):
    runtime, _, _ = setup(tmp_path)
    sampler = PerformanceSampler(runtime)
    storage = ControlledStorage(tmp_path)
    runtime.storage_guard = storage.guard

    async def run():
        await storage.set(10 * GIB)
        sampler.sample(1000)
        before = runtime.store.db.execute("SELECT * FROM equity_samples").fetchall()
        await storage.set(GIB)
        sampler.sample(61_000)
        assert runtime.store.db.execute("SELECT * FROM equity_samples").fetchall() == before
        await storage.set(4 * GIB)
        sampler.sample(121_000)
        rows = runtime.store.db.execute("SELECT * FROM equity_samples").fetchall()
        assert rows[: len(before)] == before
        assert len(rows) == len(before) + 1
        assert not runtime.store.intents()

    try:
        asyncio.run(run())
    finally:
        runtime.close()


def test_app_exposes_pressure_and_blocks_start_without_disabling_service(tmp_path):
    app = create_app(tmp_path, public_data=False)
    storage = ControlledStorage(tmp_path)
    with TestClient(app) as client:
        guard = app.state.storage_guard
        assert app.state.runtimes["TEST"].storage_guard is guard
        guard._statvfs = lambda _: storage.capacity
        storage.capacity.f_bavail = GIB
        client.portal.call(guard.refresh)
        health = client.get("/api/health").json()
        assert health["storage"]["status"] == "CRITICAL"
        assert not health["storage"]["canOpen"]
        assert health["liveExecutionEnabled"] is False
        dashboard = client.get("/api/dashboard")
        assert dashboard.status_code == 200
        assert dashboard.json()["storage"]["status"] == "CRITICAL"
        response = client.post(
            "/api/test/start", headers={"x-pm-csrf": dashboard.headers["x-pm-csrf"]}
        )
        assert response.status_code == 400
        assert "磁盘" in str(response.json())
        assert client.portal.call(lambda: app.state.runtimes["TEST"].status) == "PAUSED"
        assert not client.portal.call(lambda: app.state.runtimes["TEST"].store.intents())


def test_scheduled_retention_reclaims_only_unowned_public_cache_and_defers_critical(tmp_path):
    from test_token_storage import token

    app = create_app(tmp_path, public_data=False)
    storage = ControlledStorage(tmp_path)
    with TestClient(app) as client:
        guard = app.state.storage_guard
        guard._statvfs = lambda _: storage.capacity
        storage.capacity.f_bavail = 3 * GIB
        client.portal.call(guard.refresh)

        def exercise():
            runtime = app.state.runtimes["TEST"]
            runtime.add_tokens([token("cache-only", open=False, ends_ns=1)])
            before = runtime.store.get("business")
            app.state.maintain_public_metadata_once()
            assert "cache-only" not in runtime.tokens
            assert "cache-only" not in runtime.store.tokens()
            assert runtime.store.get("business") == before
            assert not runtime.store.intents() and not runtime.store.events()
            runtime.add_tokens([token("defer-critical", open=False, ends_ns=1)])

        client.portal.call(exercise)
        view = client.get("/api/dashboard").json()["storage"]
        assert view["status"] == "WARNING"
        assert view["metadataRetention"]["TEST"]["removed"] == 1
        storage.capacity.f_bavail = GIB
        client.portal.call(guard.refresh)
        client.portal.call(app.state.maintain_public_metadata_once)
        assert client.portal.call(
            lambda: "defer-critical" in app.state.runtimes["TEST"].store.tokens()
        )
        assert (
            client.get("/api/dashboard").json()["storage"]["metadataRetention"]["TEST"]["status"]
            == "deferred"
        )
