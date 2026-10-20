"""Unavailable public markets retain rights and isolate subscription recovery."""

import asyncio
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from pm_nautilus.market import MarketService, PublicMarketHub, resolution
from pm_nautilus.market_availability import market_state, missing_orderbook
from pm_nautilus.redemption import RedemptionService
from test_runtime import setup


def raw_market(token, *, archived=True):
    return {
        "id": token.market_id,
        "conditionId": token.condition_id,
        "clobTokenIds": [token.token_id, "2"],
        "outcomes": [token.direction, "NO"],
        "active": not archived,
        "closed": False,
        "archived": archived,
        "acceptingOrders": not archived,
        "enableOrderBook": True,
        "umaResolutionStatus": None,
        "outcomePrices": ["1", "0"],
    }


def absent_book():
    return httpx.Response(404, json={"error": "No orderbook exists for the requested token id"})


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, value):
        import json

        self.sent.append(json.loads(value))


async def idle_socket(_):
    await asyncio.Event().wait()


def test_missing_book_requires_exact_application_error():
    assert missing_orderbook(absent_book())
    for response in (
        httpx.Response(404, json={"error": "Not found"}),
        httpx.Response(404, text="No orderbook exists for the requested token id"),
        httpx.Response(403, json={"error": "No orderbook exists for the requested token id"}),
        httpx.Response(500, json={"error": "No orderbook exists for the requested token id"}),
        httpx.Response(200, json={}),
    ):
        assert not missing_orderbook(response)


@pytest.mark.parametrize(
    "change",
    [
        {"id": "another-market"},
        {"conditionId": "another-condition"},
        {"clobTokenIds": ["1", "1"]},
        {"clobTokenIds": ["01", "2"]},
        {"outcomes": ["NO", "YES"]},
        {"archived": True, "active": True},
        {"active": None},
        {"version": "v2"},
    ],
)
def test_mismatched_or_contradictory_metadata_is_not_proof(tmp_path, change):
    r, _, token = setup(tmp_path)
    try:
        with pytest.raises(ValueError):
            market_state(raw_market(token) | change, token, r.tokens.values())
    finally:
        r.close()


def test_one_false_flag_is_not_halt_proof_and_owned_countertoken_cannot_remap(tmp_path):
    r, _, token = setup(tmp_path)
    try:
        raw = raw_market(token, archived=False)
        raw["acceptingOrders"] = False
        with pytest.raises(ValueError):
            market_state(raw, token)
        raw["active"] = False
        assert market_state(raw, token) == "halted"
        counter = replace(token, token_id="3", direction="NO")
        with pytest.raises(ValueError):
            market_state(raw, token, [token, counter])
    finally:
        r.close()


def test_independent_quarantine_preserves_healthy_same_event_and_shadow(tmp_path):
    r, _, token = setup(tmp_path)
    peer = replace(token, token_id="3", market_id="different-market", condition_id="peer-condition")
    r.add_tokens([peer])
    r.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    r.book("3", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    r.books["1"].ask.consume(20_000, 1_000_000)
    r.checkpoint_book("1")
    before = deepcopy(r.business)
    native_ids = set(r.instrument_tokens)

    async def run():
        service = MarketService(r, httpx.MockTransport(lambda _: absent_book()))
        service.socket = idle_socket
        await service.sync_subscriptions()
        group = next(iter(service.tasks))
        task = service.tasks[group]
        ws = Socket()
        service.sockets[group] = ws
        r.disconnect(["1"])
        service.remember_error("streamError", ConnectionError("missing snapshot"))
        service.stream_state(group, "AWAITING_BOOKS", "ConnectionError")
        await service.observe_market_availability(raw_market(token), token)
        assert service.tasks[group] is task and not task.cancelled()
        assert service.subscription_ids(group) == ("3",)
        assert ws.sent == [{"operation": "unsubscribe", "assets_ids": ["1"]}]
        assert r.books["3"].ready
        assert service.streams[group]["state"] == "READY"
        assert service.scan_status["streamError"] is None
        assert "missing snapshot" in service.scan_status["lastStreamError"]
        assert r.market_availability["1"]["reason"] == "archived"
        assert "1" in r.monitored and "1" in r.books
        assert r.books["1"].ask.consumed == {20_000: 1_000_000}
        assert set(r.instrument_tokens) == native_ids and r.business == before
        assert resolution(raw_market(token), token) is None
        await service.close()

    try:
        asyncio.run(run())
    finally:
        r.close()


@pytest.mark.parametrize(
    "book_response", [lambda: httpx.Response(404, json={}), lambda: httpx.Response(403)]
)
def test_no_quarantine_without_corroborating_book_proof(tmp_path, book_response):
    r, _, token = setup(tmp_path)

    async def run():
        service = MarketService(r, httpx.MockTransport(lambda _: book_response()))
        service.sync_subscriptions = lambda: asyncio.sleep(0)
        await service.observe_market_availability(raw_market(token), token)
        assert not r.market_availability
        # A 404 by itself cannot override active matching metadata.
        service._availability_next_check.clear()
        await service.observe_market_availability(raw_market(token, archived=False), token)
        assert not r.market_availability
        await service.close()

    try:
        asyncio.run(run())
    finally:
        r.close()


def test_owned_resolution_poll_is_throttled_and_failures_keep_quarantine(tmp_path):
    r, _, token = setup(tmp_path)
    r.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    r.start()
    r.pause()
    before = deepcopy(r.business)
    calls = []
    failed = False

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/book":
            return absent_book()
        return httpx.Response(503) if failed else httpx.Response(200, json=raw_market(token))

    async def run():
        nonlocal failed
        service = MarketService(r, httpx.MockTransport(handler))
        service.sync_subscriptions = lambda: asyncio.sleep(0)
        redeem = RedemptionService(r, service)
        await redeem.run_once()
        assert calls == [f"/markets/{token.market_id}", "/book"]
        entry = dict(r.market_availability["1"])
        for _ in range(5):
            await redeem.run_once()
        assert len(calls) == 2
        service._availability_next_check.clear()
        failed = True
        await redeem.run_once()
        assert r.market_availability["1"]["since"] == entry["since"]
        assert r.market_availability["1"]["lastError"] == "metadata_unavailable"
        assert r.business == before and not r.business["claims"]
        await service.close()

    try:
        asyncio.run(run())
    finally:
        r.close()


def test_shared_quarantine_is_per_consumer_and_reopen_requires_fresh_snapshot(tmp_path):
    first, _, token = setup(tmp_path / "first")
    second, _, _ = setup(tmp_path / "second")
    peer = replace(token, token_id="3", market_id="peer-market", condition_id="peer-condition")
    first.add_tokens([peer])
    second.add_tokens([peer])

    def book(tid, condition, timestamp):
        return {
            "event_type": "book",
            "asset_id": tid,
            "market": condition,
            "timestamp": timestamp,
            "bids": [{"price": "0.015", "size": "100"}],
            "asks": [{"price": "0.02", "size": "50"}],
        }

    async def run():
        hub = PublicMarketHub(httpx.MockTransport(lambda _: absent_book()))
        hub.socket = idle_socket
        a, b = MarketService(first, hub=hub), MarketService(second, hub=hub)
        await hub.sync_subscriptions()
        group = next(iter(hub.tasks))
        task = hub.tasks[group]
        ws = Socket()
        hub.sockets[group] = ws
        hub.stream_state(group, "AWAITING_BOOKS")
        hub.message(book("1", token.condition_id, 20), {"1", "3"})
        hub.message(book("3", peer.condition_id, 20), {"1", "3"})
        first.books["1"].ask.consume(20_000, 1_000_000)
        first.checkpoint_book("1")
        before = deepcopy(first.business)
        await a.observe_market_availability(raw_market(token), token)
        assert hub.tasks[group] is task
        assert hub.subscription_ids(group) == ("1", "3")
        assert first.books["3"].ready and second.books["1"].ready
        assert not first.books["1"].ready
        assert not ws.sent  # Other consumer still requires this public source.
        a._availability_next_check.clear()
        await a.observe_market_availability(raw_market(token, archived=False), token)
        assert "1" not in first.market_availability
        assert first.market_recovering == {"1"}
        assert not first.books["1"].ready and second.books["1"].ready
        assert ws.sent == [{"operation": "subscribe", "assets_ids": ["1"]}]
        await hub.sync_subscriptions()
        assert not first.books["1"].ready  # No copy from old shared baseline.
        assert len(ws.sent) == 1
        # Duplicate subscribe may be idempotent; an older full snapshot must not
        # initialize a consumer missing the source's newer deltas.
        hub.message(book("1", token.condition_id, 19), {"1", "3"})
        assert not first.books["1"].ready
        assert second.books["1"].timestamp == 20_000_000
        a._availability_fresh_requested_at["1"] = asyncio.get_running_loop().time() - 31
        await hub.recover_consumer_baselines(group, ws)
        assert ws.sent[-2:] == [
            {"operation": "unsubscribe", "assets_ids": ["1"]},
            {"operation": "subscribe", "assets_ids": ["1"]},
        ]
        assert not first.books["1"].ready and not second.books["1"].ready
        assert first.market_recovering == second.market_recovering == {"1"}
        assert first.books["3"].ready and second.books["3"].ready
        assert hub.tasks[group] is task
        assert first.books["1"].ask.consumed == {20_000: 1_000_000}
        hub.message(book("1", token.condition_id, 19), {"1", "3"})
        assert not first.books["1"].ready and not second.books["1"].ready
        assert not hub.runtime.books["1"].ready  # Retain known timestamp floor after reset.
        await hub.recover_consumer_baselines(group, ws)
        assert len(ws.sent) == 3  # The next escalation also has a 30 s budget.
        hub.message(book("1", token.condition_id, 21), {"1", "3"})
        assert first.books["1"].ready and second.books["1"].ready
        assert first.books["1"].timestamp == 21_000_000
        assert not first.market_recovering and not second.market_recovering
        assert first.books["1"].ask.consumed == {20_000: 1_000_000}
        assert first.business == before
        await a.close()
        await b.close()
        await hub.close()

    try:
        asyncio.run(run())
    finally:
        first.close()
        second.close()


def test_confirmed_market_stop_quarantines_both_outcomes_and_reopens_atomically(tmp_path):
    r, _, token = setup(tmp_path)
    counter = replace(token, token_id="2", direction="NO")
    peer = replace(
        token, token_id="3", market_id="another-market", condition_id="another-condition"
    )
    r.add_tokens([counter, peer])
    for tid in ("1", "2", "3"):
        asks = [(20_000, 50_000_000)] if tid == "1" else []
        r.book(tid, [(15_000, 100_000_000)], asks)
    r.start()
    r.pause()
    assert r.business["cycles"][token.event_id]["token_id"] == "1"
    before = deepcopy(r.business)

    async def run():
        service = MarketService(r, httpx.MockTransport(lambda _: absent_book()))
        service.socket = idle_socket
        await service.sync_subscriptions()
        await service.observe_market_availability(raw_market(token), token)
        assert set(r.market_availability) == {"1", "2"}
        assert service.desired_subscription_ids() == {"3"}
        assert not service.availability_refresh_due(counter)
        assert not r.books["1"].ready and not r.books["2"].ready and r.books["3"].ready
        assert {"1", "2", "3"}.issubset(r.monitored)
        r.refresh_eligibility()
        assert "2" not in r.monitored  # Unheld unavailable peer leaves feed eligibility.
        service._availability_next_check.clear()
        await service.observe_market_availability(raw_market(token, archived=False), token)
        assert not r.market_availability
        r.refresh_eligibility()
        await service.sync_subscriptions()
        assert {"1", "2"}.issubset(service.desired_subscription_ids())
        assert service._availability_fresh_pending == {"1", "2"}
        assert r.market_recovering == {"1", "2"}
        assert not r.books["1"].ready and not r.books["2"].ready and r.books["3"].ready
        assert r.business == before
        await service.close()

    try:
        asyncio.run(run())
    finally:
        r.close()


def test_quarantine_without_remaining_cycle_still_rechecks_reopening(tmp_path):
    r, _, token = setup(tmp_path)
    r.add_tokens([replace(token, token_id="2", direction="NO")])
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/book":
            return absent_book()
        return httpx.Response(200, json=raw_market(token, archived=False))

    async def run():
        service = MarketService(r, httpx.MockTransport(handler))
        service.sync_subscriptions = lambda: asyncio.sleep(0)
        await service.observe_market_availability(raw_market(token), token)
        assert set(r.market_availability) == {"1", "2"}
        before = deepcopy(r.business)
        redeem = RedemptionService(r, service)
        await redeem.run_once()
        assert calls == ["/book"]  # Existing 300 s budget also covers orphan markers.
        service._availability_next_check.clear()
        await redeem.run_once()
        assert calls == ["/book", f"/markets/{token.market_id}"]
        assert not r.market_availability and r.market_recovering == {"1", "2"}
        assert r.business == before and not r.store.intents()
        await service.close()

    try:
        asyncio.run(run())
    finally:
        r.close()


def test_extra_availability_only_probes_have_per_run_budget_and_no_financial_effect(tmp_path):
    r, _, seed = setup(tmp_path)
    items = [
        replace(seed, token_id=str(i), market_id=f"market-{i}", condition_id=f"condition-{i}")
        for i in range(10, 43)
    ]
    r.add_tokens(items)
    by_market = {item.market_id: item for item in items}
    calls = []

    def handler(req):
        market_id = req.url.path.rsplit("/", 1)[-1]
        calls.append(market_id)
        return httpx.Response(200, json=raw_market(by_market[market_id], archived=False))

    async def run():
        service = MarketService(r, httpx.MockTransport(handler))
        service.sync_subscriptions = lambda: asyncio.sleep(0)
        r.market_availability.update(
            {
                item.token_id: {"status": "UNAVAILABLE", "since": "2026-10-20T00:00:00+00:00"}
                for item in items
            }
        )
        before = deepcopy(r.business)
        redeem = RedemptionService(r, service)
        await redeem.run_once()
        assert len(calls) == 16 and len(r.market_availability) == 17
        await redeem.run_once()
        assert len(calls) == 32 and len(r.market_availability) == 1
        await redeem.run_once()
        assert len(calls) == 33 and not r.market_availability
        assert len(set(calls)) == 33
        assert r.business == before and not r.store.intents()
        await service.close()

    try:
        asyncio.run(run())
    finally:
        r.close()


def test_invalid_refresh_keeps_prior_confirmed_quarantine(tmp_path):
    r, _, token = setup(tmp_path)

    async def run():
        service = MarketService(r, httpx.MockTransport(lambda _: absent_book()))
        service.sync_subscriptions = lambda: asyncio.sleep(0)
        await service.observe_market_availability(raw_market(token), token)
        since = r.market_availability["1"]["since"]
        for change in ({"active": None}, {"id": "replacement-market"}, {"outcomes": ["NO", "YES"]}):
            service._availability_next_check.clear()
            await service.observe_market_availability(
                raw_market(token, archived=False) | change, token
            )
            assert r.market_availability["1"]["since"] == since
            assert r.market_availability["1"]["lastError"] == "metadata_invalid"
            assert not r.market_recovering
        await service.close()

    try:
        asyncio.run(run())
    finally:
        r.close()
