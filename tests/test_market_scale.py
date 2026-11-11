"""Large-market scaling: cheaper paths must act exactly like the originals.

Measured locally (2026-11-11, synthetic 12,000 tokens, ten TEST accounts, one
RUNNING, 200 public changes per second on two cores): before these changes the
event loop saturated, lagged by seconds and never had every book ready; after
them every book was ready in about 20 seconds at roughly a third of one core.
"""

import asyncio
from random import Random
from types import SimpleNamespace

from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.enums import BookType

from pm_nautilus.market import MarketService, PublicMarketHub
from pm_nautilus.native import instrument_id
from pm_nautilus.test_market import TestMarketService
from pm_nautilus.test_runtime import TestRuntime

from test_market_efficiency import native_levels, public_book
from test_runtime import setup
from test_test_market_efficiency import complete_hub


def test_consumer_allowed_matches_original_full_intersection(tmp_path):
    rng = Random(3)
    hub = PublicMarketHub()
    try:
        for trial in range(200):
            monitored = {str(n) for n in range(300) if rng.random() < 0.6}
            unavailable = {str(n): {} for n in range(300) if rng.random() < 0.1}
            runtime = SimpleNamespace(monitored=monitored, market_availability=unavailable)
            service = MarketService.__new__(MarketService)
            service.runtime = runtime
            allowed = {str(n) for n in range(300) if rng.random() < 0.5}
            expected = allowed & hub.consumer_subscription_ids(service)
            assert hub._consumer_allowed(service, allowed) == expected
    finally:
        asyncio.run(hub.close())


def test_consumer_allowed_keeps_original_rule_for_custom_subscriptions():
    hub = PublicMarketHub()
    service = SimpleNamespace(
        runtime=SimpleNamespace(monitored={"1", "2"}, market_availability={}),
        desired_subscription_ids=lambda: {"2", "3"},
    )
    try:
        assert hub._consumer_allowed(service, {"1", "2", "3"}) == {"2", "3"}
    finally:
        asyncio.run(hub.close())


def test_stream_errors_build_each_consumer_set_once_and_match_original():
    hub = PublicMarketHub()
    calls = []

    class Consumer:
        closed = False

        def __init__(self, ids):
            self.ids = ids
            self.scan_status = {}
            self.runtime = SimpleNamespace(store=SimpleNamespace(put=lambda *a: None))

    a, b = Consumer({"1", "2"}), Consumer({"9"})
    hub.services.update({a, b})
    hub.consumer_subscription_ids = lambda service: calls.append(service) or service.ids
    for n in range(20):
        group = (f"g{n}",)
        hub.active_groups[group] = {str(n)}
        hub.streams[group] = {"state": "READY", "error": "E%d" % n if n in (1, 2, 9) else None}
    try:
        hub.update_consumer_stream_errors()
        assert sorted(map(id, calls)) == sorted(map(id, (a, b)))
        assert a.scan_status["streamError"] == "E2" and b.scan_status["streamError"] == "E9"
        calls.clear()
        for entry in hub.streams.values():
            entry["error"] = None
        hub.update_consumer_stream_errors()
        assert calls == []  # No error anywhere: no subscription set is built.
        assert a.scan_status["streamError"] is None and b.scan_status["streamError"] is None
    finally:
        asyncio.run(hub.close())


def test_ready_stream_is_not_republished_on_every_frame():
    hub = PublicMarketHub()
    published = []
    hub.update_consumer_stream_errors = lambda: published.append(True)
    group = ("g",)
    hub.active_groups[group] = {"1"}
    hub.streams[group] = {"state": "AWAITING_BOOKS", "error": "old"}
    try:
        hub.runtime.books["1"].snapshot([(10_000, 1)], [(20_000, 1)], 1)
        hub.refresh_stream_readiness(group)
        assert hub.streams[group]["state"] == "READY" and published == [True]
        for _ in range(5):
            hub.refresh_stream_readiness(group)
        assert published == [True]
    finally:
        asyncio.run(hub.close())


def periodic(service):
    service._periodic_sync = True

    async def run():
        try:
            await service.sync_subscriptions()
        finally:
            service._periodic_sync = False

    return run()


def test_periodic_requests_reuse_a_recent_plan_but_changes_plan_immediately(tmp_path, monkeypatch):
    async def run():
        async with complete_hub(tmp_path, monkeypatch, count=3, service_type=TestMarketService) as (
            hub,
            runtimes,
            services,
            sockets,
            token,
            clock,
        ):
            # Make the signature path unavailable so only the time rule can skip.
            monkeypatch.setattr(hub, "_complete_signature", lambda: None)
            await services[0].sync_subscriptions()  # Non-periodic: always plans.
            runs = hub.plan_runs
            for service in services:
                await periodic(service)
            assert hub.plan_runs == runs and hub.plan_debounces == 3
            # The requester's own subscription changed: plan at once.
            runtimes[1].update_preferences({"marketTypes": ["MULTI"]})
            await periodic(services[1])
            assert hub.plan_runs == runs + 1
            # A ready shared book the requester has not received: plan at once.
            runtimes[1].update_preferences({"marketTypes": ["BINARY", "TERNARY"]})
            await services[1].sync_subscriptions()
            runs = hub.plan_runs
            runtimes[2].disconnect([token.token_id])
            assert hub.runtime.books[token.token_id].ready
            await periodic(services[2])
            assert hub.plan_runs == runs + 1 and runtimes[2].books[token.token_id].ready
            # The window expires: the next periodic request plans again.
            runs = hub.plan_runs
            hub._last_full_plan_at -= 5
            await periodic(services[0])
            assert hub.plan_runs == runs + 1

    asyncio.run(run())


def test_live_consumer_always_uses_the_original_planning_path(tmp_path, monkeypatch):
    async def run():
        async with complete_hub(tmp_path, monkeypatch, count=2, service_type=TestMarketService) as (
            hub,
            runtimes,
            services,
            sockets,
            token,
            clock,
        ):
            monkeypatch.setattr(hub, "_complete_signature", lambda: None)
            await services[0].sync_subscriptions()

            class Live:
                runtime = SimpleNamespace(mode="LIVE")
                closed = False

            live = Live()
            assert hub._recent_plan_covers(services[1])
            hub.services.add(live)
            try:
                assert not hub._recent_plan_covers(services[1])
            finally:
                hub.services.discard(live)

    asyncio.run(run())


def applied(deltas_list, token):
    book = OrderBook(instrument_id(token), BookType.L2_MBP)
    for deltas in deltas_list:
        book.apply_deltas(deltas)
    return book


def test_paused_test_account_defers_native_copy_and_start_replays_it(tmp_path):
    reference, clock, token = setup(tmp_path / "reference")
    reference.close()
    r = TestRuntime(tmp_path / "paused.sqlite", clock=clock)
    other = TestRuntime(tmp_path / "running.sqlite", clock=clock)
    received, published = [], []
    try:
        other.native.bus.subscribe("data.book.deltas.*", published.append)
        for runtime in (r, other):
            runtime.add_tokens([token])
            runtime.update_preferences({"maxBuyPriceCents": "1"})  # Quotes never buy.
        other.start()
        service, peer = MarketService(r), MarketService(other)
        r.native.bus.subscribe("data.book.deltas.*", received.append)
        assert r.status == "PAUSED"
        rng = Random(5)
        for runtime in (r, other):
            runtime.book(token.token_id, [(15_000, 10_000_000)], [(20_000, 20_000_000)], 1000)
        for n in range(60):
            side = rng.choice(("BUY", "SELL"))
            price = (
                rng.choice(("0.012", "0.015", "0.018"))
                if side == "BUY"
                else rng.choice(("0.02", "0.024", "0.03"))
            )
            msg = dict(
                event_type="price_change",
                timestamp=str(2000 + n),
                market=token.condition_id,
                price_changes=[
                    dict(
                        asset_id=token.token_id,
                        side=side,
                        price=price,
                        size=str(rng.randrange(0, 50)),
                    )
                ],
            )
            service.message(msg, {token.token_id})
            peer.message(msg, {token.token_id})
        assert received == [] and token.token_id in r._native_deferred
        assert r.cycle_id(token) in r._test_preview_pending
        # The Python book, which every decision reads, never skipped anything.
        assert dict(r.books[token.token_id].bid.external) == dict(
            other.books[token.token_id].bid.external
        )
        assert dict(r.books[token.token_id].ask.external) == dict(
            other.books[token.token_id].ask.external
        )
        r.start()
        assert len(received) == 1 and not r._native_deferred
        # The one replayed snapshot equals every change the running peer published.
        replayed = applied(received, token)
        expected = applied(published, token)
        assert len(published) > 2
        assert native_levels(replayed, "bids") == native_levels(expected, "bids")
        assert native_levels(replayed, "asks") == native_levels(expected, "asks")
        # RUNNING publishes every change again.
        msg = dict(
            event_type="price_change",
            timestamp="9000",
            market=token.condition_id,
            price_changes=[dict(asset_id=token.token_id, side="BUY", price="0.016", size="3")],
        )
        service.message(msg, {token.token_id})
        assert len(received) == 2
        asyncio.run(service.close())
        asyncio.run(peer.close())
    finally:
        r.close()
        other.close()


def test_live_runtime_never_defers_native_quotes(tmp_path):
    r, _, token = setup(tmp_path)
    try:
        assert r.status == "PAUSED" and r.defer_native_quote(token) is False
        received = []
        r.native.bus.subscribe("data.book.deltas.*", received.append)
        r.book(token.token_id, [(15_000, 1_000_000)], [(20_000, 1_000_000)], 1000)
        assert len(received) == 1
    finally:
        r.close()


def test_hub_book_snapshot_reaches_paused_python_book_without_native_copy(tmp_path):
    reference, clock, token = setup(tmp_path / "reference")
    reference.close()
    r = TestRuntime(tmp_path / "t.sqlite", clock=clock)
    try:
        r.add_tokens([token])
        received = []
        r.native.bus.subscribe("data.book.deltas.*", received.append)
        MarketService(r).message(public_book(token), {token.token_id})
        assert r.books[token.token_id].ready and received == []
    finally:
        r.close()


def run_pause_start_quotes(path, seed, defer, monkeypatch):
    from test_hot_path_efficiency import drain_variant, runtime, state, tokens

    with monkeypatch.context() as patch:
        if not defer:
            patch.setattr(TestRuntime, "defer_native_quote", lambda self, token: False)
        with drain_variant(False):
            r, clock = runtime(path, TestRuntime)
            rng = Random(seed)
            ids = [t.token_id for t in tokens()]
            states = []
            try:
                now = clock.timestamp_ns()
                for step in range(400):
                    if step in (0, 200):
                        r.start()
                    elif step in (120, 320):
                        r.pause()
                    tid = rng.choice(ids)
                    ask = rng.randrange(10, 70) * 1000
                    bid = rng.randrange(max(1, ask // 2000), ask // 1000) * 1000
                    r.book(
                        tid,
                        [(bid, rng.randrange(1, 200) * 1_000_000)],
                        [(ask, rng.randrange(1, 200) * 1_000_000)],
                    )
                    if rng.random() < 0.3:
                        now += rng.randrange(1, 40) * 1_000_000_000
                        clock.set_time(now)
                    if step % 25 == 24:
                        r.refresh_eligibility()
                        r.drain()
                    states.append(state(r))
                return states
            finally:
                r.close()


def test_native_deferral_never_changes_a_test_decision(tmp_path, monkeypatch):
    for seed in range(4):
        deferred = run_pause_start_quotes(
            tmp_path / f"d{seed}" / "s.sqlite", seed, True, monkeypatch
        )
        original = run_pause_start_quotes(
            tmp_path / f"o{seed}" / "s.sqlite", seed, False, monkeypatch
        )
        for step, (a, b) in enumerate(zip(deferred, original, strict=True)):
            assert a == b, (seed, step)
        assert deferred[-1][0]  # The scenario traded.
