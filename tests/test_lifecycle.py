"""Shutdown and task supervision with only local stores and controlled clients."""

import asyncio
import fcntl
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

import pm_nautilus.app as app_module
from pm_nautilus.live import LiveExecution
from pm_nautilus.market import MarketService
from pm_nautilus.strategy import Runtime
from nautilus_trader.adapters.polymarket.config import PolymarketExecClientConfig
from nautilus_trader.adapters.polymarket.common.credentials import PolymarketWebSocketAuth

from test_live import HTTP, Provider
from test_runtime import setup


def controlled_factory(owner):
    client = LiveExecution(
        owner,
        HTTP(),
        Provider(),
        PolymarketWebSocketAuth("controlled", "controlled", "controlled"),
        PolymarketExecClientConfig(),
    )
    client._disconnect = AsyncMock()
    client._submit_order = AsyncMock()
    client._cancel_order = AsyncMock()
    return client


def test_failed_live_activation_closes_registered_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_LIVE_ENABLED", "true")
    monkeypatch.setenv("PM_UI_PASSWORD", "controlled-local-password")
    monkeypatch.setattr("pm_nautilus.live.load_settings", lambda: {})
    monkeypatch.setattr("pm_nautilus.live.live_factory", lambda _: controlled_factory)
    monkeypatch.setattr(
        "pm_nautilus.redemption.PolygonWallet",
        lambda _: SimpleNamespace(preflight=lambda: None),
    )
    runtimes = []

    def runtime(*args, **kwargs):
        r = Runtime(*args, **kwargs)
        runtimes.append(r)
        if r.mode == "LIVE":
            r.client.activate = AsyncMock(side_effect=ValueError("controlled activation failure"))
        return r

    monkeypatch.setattr(app_module, "Runtime", runtime)

    async def run():
        app = app_module.create_app(tmp_path, public_data=False)
        with pytest.raises(ValueError, match="controlled activation failure"):
            async with app.router.lifespan_context(app):
                pytest.fail("Activation failure must abort startup")

        assert [r.mode for r in runtimes] == ["TEST", "LIVE"]
        for r in runtimes:
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                r.store.get("status")
        live = runtimes[1]
        live.client._disconnect.assert_awaited_once()
        assert live.native.execution.get_cmd_queue_task().done()
        assert live.native.execution.get_evt_queue_task().done()
        with (tmp_path / "process.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    asyncio.run(run())


def test_live_shutdown_drains_native_event_before_sqlite_close(tmp_path):
    async def run():
        r = Runtime(tmp_path / "live.sqlite", mode="LIVE", live_factory=controlled_factory)
        reference, _, token = setup(tmp_path / "reference")
        reference.close()
        r.add_tokens([token])
        oid = r.submit(token, "BUY", 5_000_000, 990_000, cash=1_000_000)
        order = next(o for o in r.native.cache.orders() if str(o.client_order_id) == oid)

        async def final_callback():
            await asyncio.sleep(0)
            r.client.generate_order_denied(
                order.strategy_id,
                order.instrument_id,
                order.client_order_id,
                "controlled shutdown response",
                r.now,
            )
            r.store.put("callback_completed", True)

        callback = r.client.create_task(final_callback())
        await r.client.shutdown()
        assert callback.done()
        assert r.store.get("callback_completed") is True
        assert r.store.intent(oid)["terminal"] is True
        assert r.business["cursor"] == r.store.events()[-1][0]
        assert r.native.execution.get_cmd_queue_task().done()
        assert r.native.execution.get_evt_queue_task().done()
        assert all(task.done() for task in r.client._tasks)
        r.store.close()
        await asyncio.sleep(0)

    asyncio.run(run())


def test_live_shutdown_awaits_canceled_task_finalizers(tmp_path, monkeypatch):
    async def run():
        r = Runtime(tmp_path / "live.sqlite", mode="LIVE", live_factory=controlled_factory)
        entered = asyncio.Event()
        wait_forever = asyncio.Event()

        async def pending_response():
            entered.set()
            try:
                await wait_forever.wait()
            finally:
                r.store.put("response_finalized", True)

        task = r.client.create_task(pending_response())
        await entered.wait()
        original_wait = asyncio.wait

        async def no_grace_delay(tasks, timeout):
            # Exercise timeout cleanup deterministically without a five-second test sleep.
            return await original_wait(tasks, timeout=0)

        monkeypatch.setattr("pm_nautilus.live.asyncio.wait", no_grace_delay)
        await r.client.shutdown()
        assert task.cancelled()
        assert r.store.get("response_finalized") is True
        r.store.close()
        await asyncio.sleep(0)

    asyncio.run(run())


def test_market_task_failure_pauses_and_recovers_without_start(monkeypatch):
    saved = {}
    runtime = SimpleNamespace(
        status="RUNNING",
        store=SimpleNamespace(
            get=lambda key, default=None: default,
            put=lambda key, value: saved.update({key: dict(value)}),
        ),
        refresh_eligibility=Mock(side_effect=[ValueError("controlled failure"), None]),
        drain=Mock(),
    )
    runtime.pause = lambda: setattr(runtime, "status", "PAUSED")
    service = MarketService(runtime)
    service.scan = AsyncMock()
    service.sync_subscriptions = AsyncMock()
    service.on_resolution = AsyncMock()
    rounds = []

    async def finish_round(_):
        rounds.append((runtime.status, service.scan_status.get("serviceError")))
        if len(rounds) == 2:
            raise asyncio.CancelledError

    async def run():
        monkeypatch.setattr("pm_nautilus.market.asyncio.sleep", finish_round)
        try:
            with pytest.raises(asyncio.CancelledError):
                await service.run()
            assert rounds == [("PAUSED", "ValueError"), ("PAUSED", "ValueError")]
            assert saved["scan"]["serviceError"] == "ValueError"
            assert saved["scan"]["lastServiceError"] == "ValueError: controlled failure"
            assert saved["scan"]["lastServiceErrorAt"]
            service.on_resolution.assert_awaited_once()
        finally:
            await service.close()

    asyncio.run(run())


def test_closed_market_service_cannot_reopen_subscriptions_or_apply_messages(tmp_path):
    async def run():
        r, _, _ = setup(tmp_path)
        service = MarketService(r)
        service.socket = AsyncMock(side_effect=AssertionError("Closed service subscribed"))
        await service.close()
        r.close()
        # Stale messages and a configuration handler resumed after reset are inert.
        service.message({"event_type": "book", "asset_id": "1"}, {"1"})
        await service.sync_subscriptions()
        service.socket.assert_not_called()
        assert service.tasks == {}

    asyncio.run(run())


def test_health_reports_background_service_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    app = app_module.create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        service = app.state.services["TEST"]
        service.scan_status["serviceError"] = "ValueError"
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["backgroundErrors"] == {"TEST": "ValueError"}
        service.scan_status.pop("serviceError")
        assert client.get("/api/health").status_code == 200
