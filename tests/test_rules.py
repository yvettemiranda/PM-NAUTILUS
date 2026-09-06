from dataclasses import replace
import pytest
from pm_nautilus.config import Preferences
from pm_nautilus.rules import (
    Fees,
    Token,
    Target,
    StopLoss,
    plan_buy,
    sell_batches,
    target_price,
    static_reason,
)
from pm_nautilus.books import Book

D = 86_400_000_000_000


def token(**kw):
    return replace(
        Token("t", "e", "m", "c", "YES", 2, False, 0, 10 * D, Fees(False), 1_000_000, 1000), **kw
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("minBuyPriceCents", 0),
        ("maxBuyPriceCents", 99.1),
        ("minBuyPriceCents", 0.15),
        ("stopLossMultiplier", 1),
        ("stopLossMultiplier", 0),
        ("minMarketDurationDays", 1.5),
        ("maxMarketProgressPercent", 0),
        ("targetSellPriceMultiplier", -1),
        ("marketTypes", []),
    ],
)
def test_config_bounds(field, value):
    with pytest.raises(ValueError):
        Preferences(**{field: value})


def test_target_tick_cap_and_actual_fill():
    c = Preferences()
    assert target_price(21000, 1000, c) == 32000
    assert target_price(800000, 1000, c) == 990000
    assert [
        x.target
        for x in plan_buy(
            [(20000, 10_000_000), (30000, 30_000_000)],
            1_000_000,
            990000,
            5_000_000,
            Fees(False),
            1000,
            c,
        )
    ] == [30000, 45000]


def test_buy_fee_is_shares_and_no_budget_increase():
    fills = plan_buy(
        [(100_000, 100_000_000)],
        1_000_000,
        100_000,
        5_000_000,
        Fees(True, 40_000, 1),
        1000,
        Preferences(),
    )
    f = fills[0]
    assert (f.gross, f.amount, f.fee, f.net) == (10_000_000, 1_000_000, 36000, 9_640_000)
    assert (
        plan_buy(
            [(900000, 10_000_000)], 1_000_000, 990000, 5_000_000, Fees(False), 1000, Preferences()
        )
        == []
    )


def test_shared_depth_and_small_target_group():
    targets = [
        Target("a", 30000, 2_000_000, 1),
        Target("b", 35000, 3_000_000, 2),
        Target("c", 35000, 5_000_000, 3),
    ]
    batches = sell_batches([(35000, 5_000_000)], targets, 5_000_000, Fees(False))
    assert len(batches) == 1 and batches[0].limit == 35000
    assert sum(f.net for b in batches for f in b.fills) == 5_000_000


def test_unrelated_lower_bid_does_not_replenish():
    b = Book()
    b.snapshot([(35000, 5_000_000), (10000, 1_000_000)], [(20000, 50_000_000)], 1)
    ask_version = b.ask.version
    b.bid.consume(35000, 5_000_000)
    b.delta("BID", [(10000, 2_000_000)], 2)
    assert dict(b.bid.available()).get(35000, 0) == 0
    assert b.ask.version == ask_version
    b.disconnect()
    b.snapshot([(35000, 5_000_000), (10000, 2_000_000)], [(20000, 50_000_000)], 3)
    assert dict(b.bid.available()).get(35000, 0) == 0
    b.delta("BID", [(35000, 7_000_000)], 4)
    assert dict(b.bid.available())[35000] == 2_000_000


def test_preview_bid_uses_same_consumption_as_sell():
    from pm_nautilus.rules import preview

    b = Book()
    b.snapshot([(35000, 5_000_000)], [(20000, 50_000_000)], 1)
    b.bid.consume(35000, 4_000_000)
    p = preview(
        token(min_size=1_000_000),
        Preferences(),
        b.bid.available(),
        b.ask.available(),
        1_000_000,
        1_000_000,
    )
    assert p.exit_coverage == 1_000_000
    assert dict(b.bid.available())[35000] == 1_000_000


def test_stop_two_versions_30s_and_irreversible():
    s = StopLoss(True, 400000)
    s.add(100000, 10_000_000)
    s.observe(39000, "a", 0, True)
    assert s.state == "ARMED"
    s.observe(39000, "a", 40_000_000_000, True)
    assert s.state == "ARMED"
    s.observe(None, "b", 40_000_000_000, True)
    assert s.state == "ARMED"
    s.observe(39000, "b", 29_000_000_000, True)
    assert s.state == "ARMED"
    s.observe(39000, "c", 30_000_000_000, True)
    assert s.state == "EXITING"
    s.observe(90000, "d", 40_000_000_000, True)
    assert s.state == "EXITING"


def test_stop_reset_equal_and_reversed_time():
    s = StopLoss(True, 400000)
    s.add(100000, 10_000_000)
    s.observe(39000, "a", 20, True)
    s.observe(39000, "b", 10, True)
    assert s.last_ns == 20
    s.observe(40000, "b", 30, True)
    assert s.state == "WATCHING"
    s.add(200000, 10_000_000)
    assert s.threshold == 60000


def test_total_duration_and_progress():
    c = Preferences()
    assert static_reason(token(), c, D, set()) is None
    assert static_reason(token(), c, 3 * D, set()) == "PROGRESS_ABOVE_MAX"
    assert static_reason(token(ends_ns=40 * D), c, 39 * D, set()) == "DURATION_ABOVE_MAX"
    assert static_reason(token(game_start_ns=D), c, D, set()) == "GAME_START"
