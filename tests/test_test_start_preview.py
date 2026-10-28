"""Page reads cannot consume TEST trading work or change a quick PAUSE/START."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from nautilus_trader.common.component import TestClock

from pm_nautilus.app import create_app
from pm_nautilus.test_runtime import TestRuntime
from pm_nautilus.views import dashboard

from test_test_optimization_equivalence import DAY, token_set


@pytest.mark.parametrize("account_id", [1, 10])
@pytest.mark.parametrize("read", [None, "summary", "market", "preferences"])
def test_quick_pause_start_uses_new_quotes_independently_of_page_reads(
    tmp_path, monkeypatch, account_id, read
):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("PM_UI_PASSWORD", raising=False)
    clock = TestClock()
    clock.set_time(DAY)
    app = create_app(tmp_path, public_data=False, test_clock=clock)
    with TestClient(app) as client:
        initial = client.get(f"/api/dashboard?account_id={account_id}&details=summary")
        client.headers["x-pm-csrf"] = initial.headers["x-pm-csrf"]
        runtime = app.state.test_runtimes[account_id]

        def quote(bid):
            runtime.book("1", [(bid, 100_000_000)], [(20_000, 50_000_000)])

        client.portal.call(lambda: runtime.add_tokens(token_set(1)))
        client.portal.call(quote, 1000)
        assert client.post(f"/api/TEST/start?account_id={account_id}").status_code == 200
        assert client.portal.call(lambda: len(runtime.store.intents())) == 0
        assert client.post(f"/api/TEST/pause?account_id={account_id}").status_code == 200
        client.portal.call(quote, 15_000)
        assert client.portal.call(lambda: runtime._refresh_control[1]) == "RUNNING"
        assert client.portal.call(lambda: "event-0" in runtime.dirty)
        writes = client.portal.call(lambda: runtime.store.db.total_changes)
        if read is not None:
            endpoint = (
                f"/api/TEST/preferences?account_id={account_id}"
                if read == "preferences"
                else f"/api/dashboard?account_id={account_id}&details={read}"
            )
            for _ in range(3):
                response = client.get(endpoint)
                assert response.status_code == 200
                assert response.json()["accountId"] == account_id
            assert client.portal.call(lambda: runtime.store.db.total_changes) == writes
            assert client.portal.call(lambda: len(runtime.store.intents())) == 0
            assert client.portal.call(lambda: "event-0" in runtime.dirty)
        response = client.post(f"/api/TEST/start?account_id={account_id}")
        assert response.status_code == 200
        assert response.json()["strategy"]["availableCash"] == "99"
        assert client.portal.call(lambda: len(runtime.store.intents())) == 1
        assert client.get(f"/api/TEST/validation?account_id={account_id}").json()["ok"]

        def siblings_untouched():
            return all(
                not other.store.intents() and other.status == "PAUSED"
                for number, other in app.state.test_runtimes.items()
                if number != account_id
            )

        assert client.portal.call(siblings_untouched)


def test_quick_resume_after_preview_keeps_latest_finite_cash_ordering(tmp_path):
    clock = TestClock()
    clock.set_time(DAY)
    runtime = TestRuntime(tmp_path / "test.sqlite", clock=clock)
    try:
        later = replace(token_set(1)[0], event_id="later")
        earlier = replace(
            later,
            token_id="2",
            event_id="earlier",
            market_id="earlier-market",
            condition_id="earlier-condition",
            opened_ns=DAY // 2,
            ends_ns=10 * DAY + DAY // 2,
        )
        runtime.update_preferences({}, capital="1")
        runtime.add_tokens([later, earlier])
        for token in (later, earlier):
            runtime.book(token.token_id, [(1000, 100_000_000)], [(20_000, 50_000_000)])
        runtime.start()
        runtime.pause()
        for token in (later, earlier):
            runtime.book(token.token_id, [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        view = dashboard(runtime)
        assert view["marketScan"]["eventCount"] == 2
        assert {"later", "earlier"} <= runtime.dirty
        assert not runtime.test_market_pending_events()
        runtime.start()
        intents = list(runtime.store.intents().values())
        assert len(intents) == 1 and intents[0]["token_id"] == "2"
        assert runtime.cash() == 0 and runtime.validate()["ok"]
    finally:
        runtime.close()


def test_repeated_explicit_start_does_not_reuse_consumed_depth(tmp_path):
    clock = TestClock()
    clock.set_time(DAY)
    runtime = TestRuntime(tmp_path / "test.sqlite", clock=clock)
    try:
        runtime.add_tokens(token_set(1))
        runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        runtime.start()
        shadow = dict(runtime.books["1"].ask.consumed)
        facts = len(runtime.store.events()), len(runtime.store.intents()), runtime.cash()
        for _ in range(3):
            runtime.start()
        assert (len(runtime.store.events()), len(runtime.store.intents()), runtime.cash()) == facts
        assert runtime.books["1"].ask.consumed == shadow
        assert runtime.validate()["ok"]
    finally:
        runtime.close()


def test_pause_during_initial_storage_refresh_invalidates_pending_start(tmp_path, monkeypatch):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh():
            entered.set()
            await release.wait()

        guard = SimpleNamespace(can_open=True, require_safe=lambda: None, refresh=refresh)
        clock = TestClock()
        clock.set_time(DAY)
        runtime = TestRuntime(tmp_path / "test.sqlite", clock=clock, storage_guard=guard)
        pending = None
        try:
            runtime.add_tokens(token_set(1))
            runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
            validate = AsyncMock(wraps=runtime.validate_async)
            monkeypatch.setattr(runtime, "validate_async", validate)
            pending = asyncio.create_task(runtime.start_async())
            await asyncio.wait_for(entered.wait(), 3)
            runtime.pause()
            release.set()
            with pytest.raises(ValueError, match="启动核对期间状态已更改"):
                await asyncio.wait_for(pending, 3)
            validate.assert_not_awaited()
            assert runtime.status == "PAUSED"
            assert not runtime.store.intents() and runtime.cash() == 100_000_000
        finally:
            release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            runtime.close()

    asyncio.run(run())
