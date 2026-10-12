"""Deterministic public feed and calculation checks; no venue traffic."""

import asyncio
import json
from dataclasses import replace
from fractions import Fraction
from random import Random
from unittest.mock import Mock

import httpx
import pytest
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.enums import BookAction, BookType

from pm_nautilus.config import SCALE, micros
from pm_nautilus.market import MarketService, PublicMarketHub, normalize
from pm_nautilus.native import instrument_id
from pm_nautilus.rules import Fees, Target, _worst_buy_fee_ratio, sell_batches
from pm_nautilus.strategy import Runtime

from test_live_recovery import controlled_runtime, token as live_token
from test_market import category_response, event
from test_runtime import setup


def brute_fee(fees, tick, limit):
    if not fees.enabled or not fees.rate:
        return Fraction(0)
    return max(
        Fraction(
            fees.rate * p ** (fees.exponent - 1) * (SCALE - p) ** fees.exponent,
            SCALE ** (2 * fees.exponent),
        )
        if fees.exponent
        else Fraction(fees.rate, p)
        for p in range(tick, min(limit, SCALE - tick) + 1, tick)
    )


@pytest.mark.parametrize("exponent", range(11))
def test_exact_fee_peak_matches_exhaustive_legal_ticks(exponent):
    rng = Random(exponent)
    for tick in (100_000, 10_000, 1_000, 100):
        fees = Fees(True, rng.randint(1, SCALE), exponent)
        for limit in (tick, max(tick, 103_017), max(tick, 501_127), 990_000):
            assert _worst_buy_fee_ratio(fees, tick, limit) == brute_fee(fees, tick, limit)


def test_outbound_sell_group_keeps_dust_and_rounds_floor_after_aggregation():
    targets = [Target("a", 33_000, 3_004_000, 1), Target("b", 33_000, 2_008_000, 2)]
    assert not sell_batches([(35_000, 10_000_000)], targets, 5_005_000, Fees(False), tick=10_000)
    batches = sell_batches([(40_000, 10_000_000)], targets, 5_005_000, Fees(False), tick=10_000)
    assert len(batches) == 1
    assert batches[0].limit == 40_000
    assert batches[0].quantity == 5_010_000
    assert [t.qty for t in batches[0].targets] == [3_004_000, 2_008_000]
    assert sum(f.net for f in batches[0].fills) == 5_010_000


def native_levels(book, side):
    return [
        (
            micros(level.price.as_decimal()),
            sum(micros(order.size.as_decimal()) for order in level.orders()),
        )
        for level in getattr(book, side)()
    ]


def test_single_level_feed_replays_to_same_native_book_as_full_snapshot(tmp_path):
    r, _, t = setup(tmp_path)
    service = MarketService(r)
    received = []
    r.native.bus.subscribe("data.book.deltas.*", received.append)
    r.book(
        t.token_id,
        [(15_000, 10_000_000), (10_000, 5_000_000)],
        [(20_000, 20_000_000)],
        1_000_000_000,
    )
    native = OrderBook(instrument_id(t), BookType.L2_MBP)
    native.apply_deltas(received[-1])
    for n, (side, p, q) in enumerate(
        (("BUY", "0.015", "7"), ("BUY", "0.01", "0"), ("SELL", "0.021", "8"))
    ):
        service.message(
            dict(
                event_type="price_change",
                timestamp=str(1001 + n),
                market=t.condition_id,
                price_changes=[dict(asset_id=t.token_id, side=side, price=p, size=q)],
            ),
            {t.token_id},
        )
        deltas = received[-1]
        assert len(deltas.deltas) == 1
        assert deltas.deltas[0].action == (BookAction.DELETE if q == "0" else BookAction.UPDATE)
        native.apply_deltas(deltas)
    r.native.feed(t, r.books[t.token_id])
    full = OrderBook(instrument_id(t), BookType.L2_MBP)
    full.apply_deltas(received[-1])
    assert native_levels(native, "bids") == native_levels(full, "bids")
    assert native_levels(native, "asks") == native_levels(full, "asks")
    count = len(received)
    service.message(
        dict(
            event_type="price_change",
            timestamp="999",
            market=t.condition_id,
            price_changes=[dict(asset_id=t.token_id, side="BUY", price="0.015", size="999")],
        ),
        {t.token_id},
    )
    assert len(received) == count  # Out-of-order public input never reaches native.
    asyncio.run(service.close())
    r.close()


def test_unchanged_control_tick_skips_previews_but_time_and_cash_invalidate(tmp_path, monkeypatch):
    r, clock, t = setup(tmp_path)
    day = 86_400_000_000_000
    future = replace(t, token_id="2", event_id="future", opened_ns=2 * day, ends_ns=12 * day)
    r.add_tokens([future])
    r.book("1", [(15_000, 100_000_000)], [(20_000, 100_000_000)])
    r.refresh_eligibility()
    r.drain()
    evaluate = Mock(wraps=r.evaluate)
    monkeypatch.setattr(r, "evaluate", evaluate)
    for _ in range(3):
        r.refresh_eligibility()
        r.drain()
    evaluate.assert_not_called()
    clock.set_time(2 * day)
    r.refresh_eligibility()
    assert "future" in r.dirty and "2" in r.monitored
    r.drain()
    evaluate.reset_mock()
    # An account update must wake quotes which previously lacked capital.
    original_cash = r.cash
    monkeypatch.setattr(r, "cash", lambda: original_cash() + 1_000_000)
    r.refresh_eligibility()
    assert set(r.events) <= r.dirty
    r.drain()
    assert evaluate.called
    r.close()


def test_preview_cache_rechecks_test_consumption_and_preferences(tmp_path, monkeypatch):
    import pm_nautilus.strategy as strategy

    r, _, t = setup(tmp_path)
    original = strategy.preview
    calls = Mock(wraps=original)
    monkeypatch.setattr(strategy, "preview", calls)
    r.book("1", [(15_000, 100_000_000)], [(20_000, 100_000_000)])
    assert calls.call_count == 1  # Prepass and dispatch share a side-effect-free preview.
    r.books["1"].ask.consume(20_000, 100_000_000)
    r.evaluate(t.event_id, dispatch=False)
    assert calls.call_count == 2 and r.evaluations[t.event_id][0] == "NO_WINNER"
    r.preferences.minBidAskRatioPercent = 100
    r.evaluate(t.event_id, dispatch=False)
    assert calls.call_count == 3
    r.close()


def test_tied_candidate_lifecycle_still_reorders_without_new_quotes(tmp_path):
    r, clock, t = setup(tmp_path)
    day = 86_400_000_000_000
    clock.set_time(5 * day // 2)
    t = replace(t, opened_ns=2 * day, ends_ns=12 * day)
    sibling = replace(t, token_id="2", market_id="sibling", opened_ns=day, ends_ns=21 * day)
    r.add_tokens([t, sibling])
    r.book("1", [(15_000, 100_000_000)], [(20_000, 100_000_000)])
    r.book("2", [(15_000, 100_000_000)], [(20_000, 100_000_000)])
    assert r.evaluations[t.event_id][1].token.token_id == "1"
    assert t.event_id in r._time_sensitive_events
    clock.set_time(4 * day)
    r.refresh_eligibility()
    r.drain()
    assert r.evaluations[t.event_id][1].token.token_id == "2"
    assert not r.store.intents()
    r.close()


class IdleSocket:
    def __init__(self):
        self.sent = []
        self.queue = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, message):
        self.sent.append(message)

    async def recv(self):
        return await self.queue.get()


def public_book(t, timestamp=1000, bid="0.015", ask="0.02", quantity="100"):
    return dict(
        event_type="book",
        asset_id=t.token_id,
        market=t.condition_id,
        timestamp=str(timestamp),
        bids=[dict(price=bid, size=quantity)],
        asks=[dict(price=ask, size=quantity)],
    )


async def settle_tasks():
    for _ in range(5):
        await asyncio.sleep(0)


def test_shared_test_live_books_and_test_reset_do_not_reconnect_live(tmp_path, monkeypatch):
    async def run():
        sockets = []

        def connect(*args, **kwargs):
            socket = IdleSocket()
            sockets.append(socket)
            return socket

        monkeypatch.setattr("pm_nautilus.market.connect", connect)
        async with controlled_runtime(tmp_path / "live.sqlite") as live:
            t = live_token(live)
            live.add_tokens([t])
            test = Runtime(tmp_path / "test.sqlite")
            test.add_tokens([t])
            hub = PublicMarketHub()
            first = MarketService(test, hub=hub)
            second = MarketService(live, hub=hub)
            await first.sync_subscriptions()
            await settle_tasks()
            assert len(sockets) == 1
            hub.message(public_book(t), {t.token_id})
            test.books[t.token_id].ask.consume(20_000, 10_000_000)
            assert test.books[t.token_id] is not live.books[t.token_id]
            assert test.books[t.token_id].ask.available() == [(20_000, 90_000_000)]
            assert live.books[t.token_id].ask.available() == [(20_000, 100_000_000)]
            assert hub.runtime.books[t.token_id].ask.consumed == {}
            group = next(iter(hub.tasks))
            task = hub.tasks[group]
            await first.close()
            test.close()
            assert hub.tasks[group] is task and not task.done()
            assert live.books[t.token_id].ready
            fresh = Runtime(tmp_path / "fresh.sqlite")
            fresh.add_tokens([t])
            replacement = MarketService(fresh, hub=hub)
            await replacement.sync_subscriptions()
            assert fresh.books[t.token_id].ready
            assert fresh.books[t.token_id].timestamp == 1_000_000_000
            assert fresh.books[t.token_id].ask.consumed == {}
            assert len(sockets) == 1 and not hub.http.is_closed
            hub.runtime.disconnect({t.token_id})
            assert not fresh.books[t.token_id].ready and not live.books[t.token_id].ready
            hub.message(public_book(t, timestamp=999), {t.token_id})
            assert fresh.books[t.token_id].ready and live.books[t.token_id].ready
            assert fresh.books[t.token_id].timestamp == 999_000_000
            await replacement.close()
            fresh.close()
            await second.close()
            await hub.close()
            assert hub.http.is_closed and not hub.tasks

    asyncio.run(run())


def test_shared_source_isolates_consumer_failure_and_unions_different_filters(
    tmp_path, monkeypatch
):
    async def run():
        monkeypatch.setattr("pm_nautilus.market.connect", lambda *a, **k: IdleSocket())
        first, _, t = setup(tmp_path / "a")
        second, _, _ = setup(tmp_path / "b")
        extra = replace(t, token_id="2", event_id="extra")
        second.add_tokens([extra])
        unique = replace(t, token_id="3", event_id="unique")
        first.add_tokens([unique])
        first.book("1", [(15_000, 100_000_000)], [(20_000, 100_000_000)])
        hub = PublicMarketHub()
        one, two = MarketService(first, hub=hub), MarketService(second, hub=hub)
        await one.sync_subscriptions()
        assert not first.books["1"].ready  # New shared connection needs its own baseline.
        await settle_tasks()
        assert hub.runtime.monitored == {"1", "2", "3"}
        one.message = Mock(side_effect=ValueError("controlled consumer failure"))
        hub.message(public_book(t), {"1", "2"})
        assert one.scan_status["serviceError"] == "ValueError"
        assert two.scan_status.get("serviceError") is None
        assert second.books["1"].ready
        group = next(iter(hub.tasks))
        task = hub.tasks[group]
        first.monitored = set()
        await one.sync_subscriptions()
        assert hub.tasks[group] is task and hub.runtime.monitored == {"1", "2"}
        assert second.books["1"].ready
        socket = hub.sockets[group]
        assert {"operation": "unsubscribe", "assets_ids": ["3"]} in [
            json.loads(message) for message in socket.sent if message != "PING"
        ]
        await one.close()
        await two.close()
        await hub.close()
        first.close()
        second.close()

    asyncio.run(run())


def test_shared_discovery_requests_gamma_once_and_copies_each_runtime_metadata(tmp_path):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        category = category_response(request)
        if category is not None:
            return category
        return httpx.Response(200, json={"events": [event()], "next_cursor": None})

    async def run():
        first, _, _ = setup(tmp_path / "a")
        second, _, _ = setup(tmp_path / "b")
        hub = PublicMarketHub(httpx.MockTransport(handler))
        one, two = MarketService(first, hub=hub), MarketService(second, hub=hub)
        await asyncio.gather(one.scan(), two.scan())
        assert requests.count("/events/keyset") == 1
        assert first.tokens["1"] == second.tokens["1"]
        assert one.categories == two.categories and one.categories is not two.categories
        assert first.business is not second.business
        await one.close()
        await two.close()
        await hub.close()
        first.close()
        second.close()

    asyncio.run(run())


def test_late_live_subscription_inherits_latest_tick_before_cached_snapshot(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr("pm_nautilus.market.connect", lambda *a, **k: IdleSocket())
        async with controlled_runtime(tmp_path / "live.sqlite") as live:
            t = live_token(live)
            live.add_tokens([t])
            live.update_preferences({"marketTypes": ["TERNARY"]})
            test = Runtime(tmp_path / "test.sqlite")
            test.add_tokens([t])
            hub = PublicMarketHub()
            one, two = MarketService(test, hub=hub), MarketService(live, hub=hub)
            await one.sync_subscriptions()
            await settle_tasks()
            hub.message(public_book(t), {t.token_id})
            with pytest.raises(ConnectionError):
                hub.message(
                    dict(
                        event_type="tick_size_change",
                        asset_id=t.token_id,
                        market=t.condition_id,
                        new_tick_size="0.01",
                    ),
                    {t.token_id},
                )
            assert test.tokens[t.token_id].tick == 10_000
            assert live.tokens[t.token_id].tick == 1_000
            hub.message(public_book(t, timestamp=1001, bid="0.01", ask="0.02"), {t.token_id})
            live.update_preferences({"marketTypes": ["BINARY"]})
            await two.sync_subscriptions()
            assert live.tokens[t.token_id].tick == 10_000
            assert live.books[t.token_id].ready
            assert live.books[t.token_id].timestamp == 1_001_000_000
            assert live.books[t.token_id].ask.external == {20_000: 100_000_000}
            assert live.status == "PAUSED" and not live.store.intents()
            await one.close()
            await two.close()
            await hub.close()
            test.close()

    asyncio.run(run())


def test_discovered_tick_change_invalidates_all_consumers_without_reconnecting(
    tmp_path, monkeypatch
):
    raw = event()

    def handler(request):
        category = category_response(request)
        if category is not None:
            return category
        return httpx.Response(200, json={"events": [raw], "next_cursor": None})

    async def run():
        sockets = []

        def connect(*args, **kwargs):
            socket = IdleSocket()
            sockets.append(socket)
            return socket

        monkeypatch.setattr("pm_nautilus.market.connect", connect)
        first, clock, _ = setup(tmp_path / "first")
        second, other_clock, _ = setup(tmp_path / "second")
        t = normalize(raw)[0]
        for runtime, runtime_clock in ((first, clock), (second, other_clock)):
            runtime_clock.set_time(t.opened_ns + 86_400_000_000_000)
            runtime.add_tokens(normalize(raw))
        hub = PublicMarketHub(httpx.MockTransport(handler))
        one, two = MarketService(first, hub=hub), MarketService(second, hub=hub)
        await one.scan()
        await settle_tasks()
        hub.message(public_book(t, bid="0.025", ask="0.03"), {t.token_id})
        first.books[t.token_id].ask.consume(30_000, 3_000_000)
        group = next(iter(hub.tasks))
        task = hub.tasks[group]
        raw["markets"][0]["orderPriceMinTickSize"] = "0.01"
        hub.discovered_at = None
        await one.scan()
        assert hub.runtime.tokens[t.token_id].tick == 10_000
        assert not hub.runtime.books[t.token_id].ready
        for runtime in (first, second):
            assert runtime.tokens[t.token_id].tick == 10_000
            assert not runtime.books[t.token_id].ready
            assert not runtime.store.intents()
        assert hub.tasks[group] is task and len(sockets) == 1
        hub.message(
            dict(
                event_type="price_change",
                timestamp="1001",
                market=t.condition_id,
                price_changes=[dict(asset_id=t.token_id, side="BUY", price="0.025", size="999")],
            ),
            {t.token_id},
        )
        assert not first.books[t.token_id].ready and not second.books[t.token_id].ready
        assert first.books[t.token_id].bid.external == {25_000: 100_000_000}
        hub.message(public_book(t, timestamp=1002, bid="0.02", ask="0.03"), {t.token_id})
        assert first.books[t.token_id].ready and second.books[t.token_id].ready
        assert first.books[t.token_id].ask.consumed == {30_000: 3_000_000}
        assert not second.books[t.token_id].ask.consumed
        await one.close()
        await two.close()
        await hub.close()
        first.close()
        second.close()

    asyncio.run(run())


def test_scan_cannot_roll_back_tick_received_after_an_earlier_gamma_page(tmp_path, monkeypatch):
    async def run():
        second_page = asyncio.Event()
        release_page = asyncio.Event()
        raw = event()

        async def handler(request):
            category = category_response(request)
            if category is not None:
                return category
            if request.url.params.get("after_cursor") == "second":
                second_page.set()
                await release_page.wait()
                return httpx.Response(200, json={"events": [], "next_cursor": None})
            return httpx.Response(200, json={"events": [raw], "next_cursor": "second"})

        monkeypatch.setattr("pm_nautilus.market.connect", lambda *a, **k: IdleSocket())
        runtime, clock, _ = setup(tmp_path)
        t = normalize(raw)[0]
        clock.set_time(t.opened_ns + 86_400_000_000_000)
        runtime.add_tokens(normalize(raw))
        hub = PublicMarketHub(httpx.MockTransport(handler))
        service = MarketService(runtime, hub=hub)
        await service.sync_subscriptions()
        await settle_tasks()
        hub.message(public_book(t), {t.token_id})
        scan = asyncio.create_task(service.scan())
        await asyncio.wait_for(second_page.wait(), 1)
        with pytest.raises(ConnectionError):
            hub.message(
                dict(
                    event_type="tick_size_change",
                    asset_id=t.token_id,
                    market=t.condition_id,
                    new_tick_size="0.01",
                ),
                {t.token_id},
            )
        hub.message(public_book(t, timestamp=1001, bid="0.01", ask="0.02"), {t.token_id})
        release_page.set()
        await scan
        assert hub.runtime.tokens[t.token_id].tick == 10_000
        assert runtime.tokens[t.token_id].tick == 10_000
        assert runtime.books[t.token_id].ready and hub.runtime.books[t.token_id].ready
        assert runtime.books[t.token_id].timestamp == 1_001_000_000
        assert runtime.books[t.token_id].bid.external == {10_000: 100_000_000}
        assert runtime.store.tokens()[t.token_id].tick == 10_000
        assert not runtime.store.intents()
        assert hub._scan_tick_versions is None
        await service.close()
        await hub.close()
        runtime.close()

    asyncio.run(run())
