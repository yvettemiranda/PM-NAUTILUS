"""Real API concurrency regressions; all ledgers and LIVE transports are local fixtures."""

import asyncio
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from nautilus_trader.common.component import TestClock

from pm_nautilus.app import create_app
from pm_nautilus.rules import Fees, Token

DAY = 86_400_000_000_000
RESET = {"confirmation": "RESET TEST", "finalConfirmation": "RESET TEST AGAIN"}


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("PM_UI_PASSWORD", raising=False)


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def authenticate(client):
    response = await client.get("/api/dashboard")
    client.headers["x-pm-csrf"] = response.headers["x-pm-csrf"]


async def queued_pause(factory, path):
    app = factory(path, public_data=False)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await authenticate(client)
            runtime = app.state.runtimes["TEST"]
            original = runtime.validate_async
            entered, release = asyncio.Event(), asyncio.Event()
            calls = 0

            async def gated():
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                return await original()

            runtime.validate_async = gated
            reader = asyncio.create_task(client.get("/api/TEST/validation?account_id=1"))
            await entered.wait()
            start = asyncio.create_task(client.post("/api/TEST/start?account_id=1"))
            for _ in range(12):
                await asyncio.sleep(0)
            before_pause = {"start_done": start.done(), "validation_entries": calls}
            paused = await client.post("/api/TEST/pause?account_id=1")
            acknowledged = runtime.status
            release.set()
            validation_result, start_result = await asyncio.gather(reader, start)
            return {
                "before_pause": before_pause,
                "later_pause_http": paused.status_code,
                "later_pause_status": acknowledged,
                "validation_http": validation_result.status_code,
                "older_start_http": start_result.status_code,
                "older_start_error": start_result.json().get("error"),
                "final_status": runtime.status,
                "validation_entries": calls,
            }


async def queued_reset_generation(path):
    app = create_app(path, public_data=False)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await authenticate(client)
            old = app.state.runtimes["TEST"]
            old_generation = old.store.generation
            original = old.validate_async
            entered, release = asyncio.Event(), asyncio.Event()

            async def gated():
                entered.set()
                await release.wait()
                return await original()

            old.validate_async = gated
            reader = asyncio.create_task(client.get("/api/TEST/validation?account_id=1"))
            await entered.wait()
            reset = asyncio.create_task(client.post("/api/TEST/reset?account_id=1", json=RESET))
            for _ in range(12):
                await asyncio.sleep(0)
            start = asyncio.create_task(client.post("/api/TEST/start?account_id=1"))
            for _ in range(12):
                await asyncio.sleep(0)
            before = {
                "reset_done": reset.done(),
                "start_done": start.done(),
                "runtime_is_old": app.state.runtimes["TEST"] is old,
            }
            release.set()
            validation_result, reset_result, start_result = await asyncio.gather(
                reader, reset, start
            )
            current = app.state.runtimes["TEST"]
            return {
                "requests": "validation holds lock; RESET queued first; START queued second",
                "before_release": before,
                "validation_http": validation_result.status_code,
                "reset_http": reset_result.status_code,
                "reset_response_status": reset_result.json()["strategy"]["status"],
                "start_http": start_result.status_code,
                "generation_changed": current.store.generation != old_generation,
                "runtime_changed": current is not old,
                "final_status": current.status,
            }


async def start_during_shutdown(factory, path, queued=False):
    clock = TestClock()
    clock.set_time(DAY)
    app = factory(path, public_data=False, test_clock=clock)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    try:
        async with client_for(app) as client:
            await authenticate(client)
            runtime = app.state.runtimes["TEST"]
            token = Token(
                "1",
                "event",
                "market",
                "condition",
                "YES",
                2,
                False,
                0,
                10 * DAY,
                Fees(False),
                5_000_000,
                1000,
            )
            runtime.add_tokens([token])
            runtime.book("1", [(15_000, 100_000_000)], [(20_000, 100_000_000)])
            original_validate = runtime.validate_async
            original_finish = runtime._finish_start
            entered, release = asyncio.Event(), asyncio.Event()
            starts = []

            async def gated():
                entered.set()
                await release.wait()
                return await original_validate()

            def observe_finish(result):
                original_finish(result)
                starts.append(
                    {
                        "status": runtime.status,
                        "cash": runtime.cash(),
                        "intent_count": len(runtime.store.intents()),
                    }
                )

            runtime.validate_async = gated
            runtime._finish_start = observe_finish
            reader = None
            if queued:
                reader = asyncio.create_task(client.get("/api/TEST/validation?account_id=1"))
                await entered.wait()
            start = asyncio.create_task(client.post("/api/TEST/start?account_id=1"))
            if queued:
                for _ in range(12):
                    await asyncio.sleep(0)
            else:
                await entered.wait()
            stopping = asyncio.create_task(lifespan.__aexit__(None, None, None))
            for _ in range(12):
                await asyncio.sleep(0)
            before = {"shutdown_done": stopping.done(), "store_closed": runtime.store._closed}
            release.set()
            outcomes = await asyncio.gather(
                start, stopping, *([reader] if reader else []), return_exceptions=True
            )
            response, stop_result = outcomes[:2]
            if isinstance(stop_result, BaseException):
                raise stop_result
            with sqlite3.connect(path / "TEST" / "state.sqlite") as db:
                fills = db.execute(
                    "SELECT count(*) FROM native_events WHERE kind='OrderFilled'"
                ).fetchone()[0]
            return {
                "before_release": before,
                "start_http": None if isinstance(response, BaseException) else response.status_code,
                "start_error": type(response).__name__
                if isinstance(response, BaseException)
                else response.json().get("error"),
                "start_completions_during_shutdown": starts,
                "filled_events_after_shutdown": fills,
            }
    except BaseException:
        await lifespan.__aexit__(None, None, None)
        raise


async def live_pause_waits_for_test_lock(factory, path):
    # The real LIVE engine is created with the repository's controlled HTTP,
    # provider and AsyncMock submit/cancel/disconnect fixture. No real credentials.
    from test_lifecycle import controlled_factory

    async def idle():
        await asyncio.Future()

    def safe_factory(owner):
        client = controlled_factory(owner)

        async def activate():
            client.ready = client.open_orders_clear = True
            client.reconciliation_task = asyncio.create_task(idle())

        client.activate = AsyncMock(side_effect=activate)
        return client

    with (
        patch.dict(
            os.environ, {"PM_LIVE_ENABLED": "true", "PM_UI_PASSWORD": "controlled-local-password"}
        ),
        patch("pm_nautilus.live.load_settings", lambda: {}),
        patch("pm_nautilus.live.live_factory", lambda *_: safe_factory),
        patch(
            "pm_nautilus.redemption.PolygonWallet",
            lambda _: SimpleNamespace(preflight=lambda: None),
        ),
    ):
        app = factory(path, public_data=False)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        async with client_for(app) as client:
            client.auth = ("pm", "controlled-local-password")
            await authenticate(client)
            test = app.state.runtimes["TEST"]
            live = app.state.runtimes["LIVE"]
            live.store.put("status", "RUNNING")
            entered, release = asyncio.Event(), asyncio.Event()
            original_validate, original_pause = test.validate_async, live.pause
            pause_calls = []

            async def gated():
                entered.set()
                await release.wait()
                return await original_validate()

            def observe_pause():
                pause_calls.append(True)
                return original_pause()

            test.validate_async = gated
            live.pause = observe_pause
            # LIVE ownership now rejects new TEST validation through the API.
            # Retain this regression's older in-flight lock directly so shutdown
            # must still drain LIVE before waiting on any TEST owner.
            rejected = await client.get("/api/TEST/validation?account_id=1")
            assert rejected.status_code == 400

            async def held_validation():
                async with app.state.test_accounts.contexts[1].lock:
                    return await test.validate_async()

            reader = asyncio.create_task(held_validation())
            await asyncio.wait_for(entered.wait(), 5)
            stopping = asyncio.create_task(lifespan.__aexit__(None, None, None))
            for _ in range(20):
                await asyncio.sleep(0)
            before = {
                "live_pause_called": bool(pause_calls),
                "live_store_closed": live.store._closed,
                "live_status_if_open": None if live.store._closed else live.status,
                "test_lock_still_waiting": not reader.done(),
            }
            release.set()
            outcomes = await asyncio.gather(reader, stopping, return_exceptions=True)
            if isinstance(outcomes[1], BaseException):
                raise outcomes[1]
            return {
                "before_test_validation_released": before,
                "live_pause_called_after_release": bool(pause_calls),
                "live_store_closed_after_shutdown": live.store._closed,
            }


def test_queued_start_cannot_override_later_pause(tmp_path):
    result = asyncio.run(queued_pause(create_app, tmp_path))
    assert result["before_pause"]["start_done"] is False
    assert result["later_pause_http"] == 200
    assert result["older_start_http"] == 400
    assert result["validation_entries"] == 1
    assert result["final_status"] == "PAUSED"


def test_queued_start_cannot_cross_reset_generation(tmp_path):
    result = asyncio.run(queued_reset_generation(tmp_path))
    assert result["reset_http"] == 200
    assert result["generation_changed"] and result["runtime_changed"]
    assert result["start_http"] == 400
    assert result["final_status"] == "PAUSED"


@pytest.mark.parametrize("queued", [False, True])
def test_accepted_start_cannot_trade_after_shutdown_begins(tmp_path, queued):
    result = asyncio.run(start_during_shutdown(create_app, tmp_path, queued))
    assert result["before_release"]["shutdown_done"] is False
    assert result["start_http"] in {400, 503}
    assert result["start_completions_during_shutdown"] == []
    assert result["filled_events_after_shutdown"] == 0


def test_live_shutdown_does_not_wait_for_test_validation(tmp_path):
    result = asyncio.run(live_pause_waits_for_test_lock(create_app, tmp_path))
    before = result["before_test_validation_released"]
    assert before["test_lock_still_waiting"] is True
    assert before["live_pause_called"] is True
    assert before["live_store_closed"] is True
    assert result["live_store_closed_after_shutdown"] is True
