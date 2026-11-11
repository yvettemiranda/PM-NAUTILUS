import asyncio
from dataclasses import replace

import pytest
from nautilus_trader.common.component import TestClock
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.objects import AccountBalance, Money

from pm_nautilus.redemption import RedemptionService
from pm_nautilus.rules import Fees, Token
from pm_nautilus.test_runtime import TestRuntime
from pm_nautilus.views import dashboard

from test_live_recovery import accept, controlled_runtime, flush, token, venue_trade


DAY = 86_400_000_000_000


def market_tokens(number, *, independent=False):
    yes = Token(
        str(number * 2 - 1),
        "event",
        f"market-{number}",
        f"condition-{number}",
        "YES",
        2,
        False,
        0,
        10 * DAY,
        Fees(False),
        5_000_000,
        1_000,
        independent_market=independent,
    )
    return yes, replace(yes, token_id=str(number * 2), direction="NO")


def quote(runtime, items, *, buy=True):
    for t in items:
        runtime.book(
            t.token_id,
            [(15_000 if buy else 30_000, 100_000_000)],
            [(20_000, 50_000_000)] if buy else [],
        )


@pytest.mark.parametrize(
    "split,independent,expected",
    [
        (False, False, {"event"}),
        (True, False, {"condition:condition-1", "condition:condition-2"}),
        (False, True, {"market:market-1", "market:market-2"}),
    ],
)
def test_markets_in_same_event_choose_one_side_per_configured_scope(
    tmp_path, split, independent, expected
):
    clock = TestClock()
    clock.set_time(DAY)
    r = TestRuntime(tmp_path / "state.sqlite", clock=clock)
    try:
        r.update_preferences(
            {"cyclePerMarket": split, "includeIndependentMultiMarkets": independent}
        )
        items = market_tokens(1, independent=independent) + market_tokens(
            2, independent=independent
        )
        r.add_tokens(items)
        quote(r, items)
        candidates = dashboard(r)["marketScan"]["events"]
        assert {e.get("cycleId", e["eventId"]) for e in candidates} == expected
        assert all(e["eventId"] == "event" for e in candidates)
        r.start()
        assert set(r.business["cycles"]) == expected
        bought = {r.tokens[c["token_id"]].condition_id for c in r.business["cycles"].values()}
        assert len(bought) == len(expected)
        assert {t["event_id"] for t in r.business["targets"].values()} == expected
        assert r.cash() == (100 - len(expected)) * 1_000_000
        assert r.validate()["ok"]
    finally:
        r.close()


@pytest.mark.parametrize("split_before", [False, True])
def test_scope_switch_keeps_old_positions_and_blocks_overlap_through_restart(
    tmp_path, split_before
):
    clock = TestClock()
    clock.set_time(DAY)
    path = tmp_path / "state.sqlite"
    r = TestRuntime(path, clock=clock)
    try:
        r.update_preferences({"cyclePerMarket": split_before})
        first = market_tokens(1) + market_tokens(2)
        third = market_tokens(3)
        r.add_tokens(first)
        quote(r, first)
        r.start()
        before = {key: c["token_id"] for key, c in r.business["cycles"].items()}
        assert len(before) == (2 if split_before else 1)
        r.update_preferences({"cyclePerMarket": not split_before})
        r.add_tokens(third)
        quote(r, third)
        assert {key: c["token_id"] for key, c in r.business["cycles"].items()} == before
        r.close()
        r = None
        r = TestRuntime(path, clock=clock)
        assert r.preferences.cyclePerMarket is not split_before
        quote(r, first + third)
        r.start()
        assert {key: c["token_id"] for key, c in r.business["cycles"].items()} == before
        r.pause()
        quote(r, first, buy=False)
        assert not r.business["cycles"]
        assert not r.business["targets"]
        r.start()
        key = "event" if split_before else "condition:condition-3"
        assert set(r.business["cycles"]) == {key}
        assert r.business["cycles"][key]["token_id"] in {t.token_id for t in third}
        assert r.validate()["ok"]
    finally:
        if r is not None:
            r.close()


def test_one_market_settlement_keeps_sibling_market_rights(tmp_path):
    clock = TestClock()
    clock.set_time(DAY)
    r = TestRuntime(tmp_path / "state.sqlite", clock=clock)
    try:
        r.update_preferences({"includeIndependentMultiMarkets": True})
        first, second = market_tokens(1, independent=True), market_tokens(2, independent=True)
        r.add_tokens(first + second)
        quote(r, first + second)
        r.start()
        r.pause()
        # Disabling discovery of this kind of market cannot orphan its rights.
        r.update_preferences({"includeIndependentMultiMarkets": False})
        settle = RedemptionService(r, None)
        held = r.tokens[r.business["cycles"]["market:market-1"]["token_id"]]
        settle.identify(held, {t.token_id: (1_000_000 if t == held else 0) for t in first})
        claim = r.business["claims"][first[0].condition_id]
        assert claim["event_id"] == "market:market-1"
        assert {t["event_id"] for t in r.business["targets"].values()} == {"market:market-2"}
        asyncio.run(settle.advance(claim))
        asyncio.run(settle.advance(claim))
        assert claim["state"] == "CREDITED"
        assert set(r.business["cycles"]) == {"market:market-2"}
        assert r.cash() == 148_000_000
        assert r.validate()["ok"]
    finally:
        r.close()


def test_live_inflight_buy_projects_frozen_key_after_scope_switch(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "live.sqlite") as r:
            r.client.generate_account_state(
                [AccountBalance(Money(3, pUSD), Money(0, pUSD), Money(3, pUSD))],
                [],
                True,
                r.now,
            )
            await flush()
            first = token(r)
            sibling = replace(token(r, 2), event_id=first.event_id)
            r.add_tokens([first])
            r.book(first.token_id, [(90_000, 100_000_000)], [(100_000, 10_000_000)])
            r.client.ready = r.client.open_orders_clear = True
            r.start()
            await flush()
            oid = next(iter(r.store.intents()))
            assert r.store.intent(oid)["event_id"] == first.event_id
            await accept(r, oid)
            r.update_preferences({"cyclePerMarket": True})
            r.add_tokens([sibling])
            quote(r, [sibling])
            assert len(r.store.intents()) == 1
            r.client.ingest_trade(venue_trade(first))
            await flush()
            assert set(r.business["cycles"]) == {first.event_id}
            assert r.cycle_id(first) == first.event_id
            assert r.cycle_id(sibling) == f"condition:{sibling.condition_id}"
            assert not r._test_queue_errors
            assert r.validate()["ok"]

    asyncio.run(run())
