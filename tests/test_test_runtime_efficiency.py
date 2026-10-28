"""TEST scheduling saves unowned previews without weakening owned work or START."""

import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest
from nautilus_trader.common.component import TestClock

from pm_nautilus.rules import Token, Fees
from pm_nautilus.strategy import Runtime
from pm_nautilus.test_runtime import TestRuntime, TestTokenRegistry, create_runtime
from pm_nautilus.views import dashboard

from test_lifecycle import controlled_factory

DAY = 86_400_000_000_000


def make_runtime(path, kind=TestRuntime):
    clock = TestClock()
    clock.set_time(DAY)
    runtime = kind(path / "test.sqlite", clock=clock)
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
    return runtime, clock, token


def quote(runtime, token="1", bid=15_000, ask=20_000, bid_size=100_000_000):
    runtime.book(token, [(bid, bid_size)], [(ask, 50_000_000)])


def test_paused_empty_account_preserves_quotes_and_defers_only_candidate_work(
    tmp_path, monkeypatch
):
    runtime, _, _ = make_runtime(tmp_path)
    try:
        evaluate = Mock(wraps=runtime.evaluate)
        feed = Mock(wraps=runtime.native.feed)
        monkeypatch.setattr(runtime, "evaluate", evaluate)
        monkeypatch.setattr(runtime.native, "feed", feed)
        writes = runtime.store.db.total_changes
        for number in range(1000):
            quote(runtime, bid_size=100_000_000 + number)
        assert feed.call_count == 1000
        assert runtime.books["1"].bid.external == {15_000: 100_000_999}
        assert runtime.books["1"].revision == 1000
        assert runtime.store.db.total_changes == writes
        evaluate.assert_not_called()
        summary = dashboard(runtime, limit=0)
        assert summary["marketScan"]["eventCount"] == 0
        assert summary["marketScan"]["pendingEventCount"] == 1
        evaluate.assert_not_called()
        detail = dashboard(runtime, limit=20)
        assert detail["marketScan"]["eventCount"] == 1
        assert evaluate.call_count == 1
        assert not runtime.store.events() and not runtime.native.cache.orders()
        assert runtime.store.db.total_changes == writes
        # An earlier READY must not survive as a confirmed summary after a quote
        # makes that opportunity untradeable; detail recomputes the latest book.
        quote(runtime, bid=1000)
        summary = dashboard(runtime, limit=0)
        assert summary["marketScan"]["eventCount"] == 0
        assert summary["marketScan"]["pendingEventCount"] == 1
        detail = dashboard(runtime, limit=20)
        assert detail["marketScan"]["eventCount"] == 0
        assert detail["marketScan"]["events"][0]["status"] == "NO_WINNER"
    finally:
        runtime.close()


def test_paused_full_exit_and_callback_keep_unowned_previews_dirty(tmp_path, monkeypatch):
    runtime, _, token = make_runtime(tmp_path)
    try:
        quote(runtime)
        runtime.start()
        runtime.pause()
        other = replace(
            token,
            token_id="2",
            event_id="other",
            market_id="other-market",
            condition_id="other-condition",
        )
        runtime.add_tokens([other])
        quote(runtime, "2")
        dashboard(runtime)
        assert "other" not in runtime.dirty
        original_fill = runtime.apply_fill

        def callback_fill(event, intent, current_token):
            original_fill(event, intent, current_token)
            runtime.dirty.add("other")

        monkeypatch.setattr(runtime, "apply_fill", callback_fill)
        quote(runtime, bid=35_000, bid_size=50_000_000)
        assert not runtime.business["cycles"]
        assert len(runtime.native.cache.orders()) == 2
        assert {"event", "other"} <= runtime.dirty
        assert runtime.cash() == 100_750_000
        assert runtime.validate()["ok"]
        before = runtime.store.db.total_changes
        dashboard(runtime)
        assert runtime.store.db.total_changes == before
        assert len(runtime.native.cache.orders()) == 2
    finally:
        runtime.close()


def test_paused_active_intent_without_position_is_not_deferred(tmp_path, monkeypatch):
    runtime, _, _ = make_runtime(tmp_path)
    try:
        runtime.store.save_intent(
            "PM-TEST-pending",
            {
                "event_id": "event",
                "token_id": "1",
                "side": "BUY",
                "cash": 1_000_000,
                "spent": 0,
                "terminal": False,
                "generation": runtime.store.generation,
            },
        )
        evaluate = Mock(wraps=runtime.evaluate)
        monkeypatch.setattr(runtime, "evaluate", evaluate)
        quote(runtime)
        assert evaluate.call_count == 2
        assert all(call.args[0] == "event" for call in evaluate.call_args_list)
        assert not runtime.native.cache.orders()
    finally:
        runtime.close()


def test_start_recomputes_latest_ready_events_before_cross_event_cash_allocation(tmp_path):
    runtimes = []
    try:
        for number, kind in enumerate((Runtime, TestRuntime)):
            runtime, _, token = make_runtime(tmp_path / str(number), kind)
            runtimes.append(runtime)
            runtime.update_preferences({}, capital="1")
            other = replace(
                token,
                token_id="2",
                event_id="earlier",
                market_id="other-market",
                condition_id="other-condition",
                opened_ns=DAY // 2,
                ends_ns=10 * DAY + DAY // 2,
            )
            runtime.add_tokens([other])
            quote(runtime)
            quote(runtime, "2", bid=1000)
            dashboard(runtime)
            # Late data makes the earlier-ranked event READY. The prior visible
            # READY event must not consume the only budget when START is clicked.
            quote(runtime, "2")
            runtime.start()
        for runtime in runtimes:
            intents = list(runtime.store.intents().values())
            assert len(intents) == 1
            assert intents[0]["event_id"] == "earlier" and intents[0]["token_id"] == "2"
            assert runtime.cash() == 0
            assert runtime.business["cycles"]["earlier"]["quantity"] == 50_000_000
            assert runtime.validate()["ok"]
        assert runtimes[0].books["2"].ask.consumed == runtimes[1].books["2"].ask.consumed
    finally:
        for runtime in runtimes:
            runtime.close()


def test_registry_epoch_covers_mutations_without_losing_condition_index(tmp_path):
    runtime, _, token = make_runtime(tmp_path)
    try:
        registry = runtime.tokens
        assert isinstance(registry, TestTokenRegistry)
        version = registry.test_version
        registry["1"] = token
        assert registry.test_version == version
        other = replace(token, token_id="2", condition_id="other")
        registry.update({"2": other})
        assert registry.test_version == version + 1
        assert registry.condition_token_ids("other") == ("2",)
        registry["2"] = replace(other, tick=10_000)
        assert registry.test_version == version + 2
        registry.pop("2")
        assert registry.test_version == version + 3
        assert registry.condition_token_ids("other") == ()
        registry.setdefault("2", other)
        registry.popitem()
        assert registry.test_version == version + 5
        registry.clear()
        assert registry.test_version == version + 6
        registry.clear()
        assert registry.test_version == version + 6
    finally:
        runtime.close()


def test_factory_keeps_live_on_the_original_runtime_and_registry(tmp_path):
    async def run():
        runtime = create_runtime(
            tmp_path / "live.sqlite", mode="LIVE", live_factory=controlled_factory
        )
        try:
            assert type(runtime) is Runtime
            assert not hasattr(runtime, "refresh_test_market_preview")
            assert not hasattr(runtime.tokens, "test_version")
            dashboard(runtime, live_enabled=True)
        finally:
            await runtime.client.shutdown()
            runtime.store.close()

    asyncio.run(run())
    with pytest.raises(ValueError, match="TEST"):
        TestRuntime(tmp_path / "never-created.sqlite", mode="LIVE")
    assert not (tmp_path / "never-created.sqlite").exists()
