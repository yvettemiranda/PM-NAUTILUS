"""Exact budget, batch, content-version and qualification regression boundaries."""

from dataclasses import asdict, replace
from hashlib import sha256
from random import Random

import pytest

from pm_nautilus import books, rules
from pm_nautilus.books import Book
from pm_nautilus.config import Preferences
from pm_nautilus.order_terms import canonical_buy_taker
from pm_nautilus.rules import Fees, Target, plan_buy, preview, sell_batches, signed_buy_notional
from test_rules import token, D


def test_signed_notional_is_maximal_cent_amount_under_exact_fee_bound():
    rng = Random(731)
    for _ in range(300):
        tick = rng.choice((100_000, 10_000, 1_000, 100))
        limit = rng.randrange(1, 1_000_000 // tick) * tick
        fee = Fees(True, rng.randrange(1_000_001), rng.randrange(11))
        budget = rng.randrange(1, 2_000_001)
        ratio = rules._worst_buy_fee_ratio(fee, tick, limit)
        valid = [
            maker
            for maker in range(0, budget + 1, 10_000)
            if maker + rules.ceil_div(maker * ratio.numerator, ratio.denominator) <= budget
        ]
        assert signed_buy_notional(budget, fee, tick, limit) == max(valid)
    assert signed_buy_notional(9_999, Fees(False), 1_000, 990_000) == 0
    assert signed_buy_notional(1_000_000_000_000, Fees(False), 1_000, 990_000) == 1_000_000_000_000


def test_full_buy_quantity_skips_search_but_oversized_quote_still_honors_budget(monkeypatch):
    calls = []
    original = rules.cost

    def counted(p, q):
        calls.append((p, q))
        return original(p, q)

    monkeypatch.setattr(rules, "cost", counted)
    fee = Fees(True, 40_000, 1)
    fills = plan_buy(
        [(100_000, 100_000_000)], 1_000_000, 100_000, 5_000_000, fee, 1_000, Preferences()
    )
    assert [(f.gross, f.amount, f.fee) for f in fills] == [(9_600_000, 994_560, 34_560)]
    assert len(calls) == 2
    # A deliberately generous quote cap exercises the budget-limited path.
    fills = plan_buy(
        [(100_000, 100_000_000)],
        1_000_000,
        100_000,
        5_000_000,
        fee,
        1_000,
        Preferences(),
        2_000_000,
    )
    assert len(fills) == 1
    fill = fills[0]
    assert fill.amount <= 1_000_000
    assert original(fill.price, fill.gross + 1) + fee.fee(fill.gross + 1, fill.price) > 1_000_000


@pytest.mark.parametrize(
    "tick,ask,expected",
    [
        (100_000, 300_000, 3_333_000),
        (10_000, 30_000, 33_333_300),
        (1_000, 21_000, 47_619_040),
        (100, 12_300, 81_300_813),
    ],
)
def test_buy_precision_matches_tick_and_minimum_gate(tick, ask, expected):
    t = token(tick=tick, min_size=expected - 1)
    result = preview(
        t, Preferences(), [(ask, 100_000_000)], [(ask, 100_000_000)], 1_000_000, 1_000_000
    )
    assert result is not None
    assert result.quantity == expected == canonical_buy_taker(1_000_000, ask, tick)
    assert result.spent <= 1_000_000
    partial = plan_buy(
        [(ask, expected - 100)], 1_000_000, ask, t.min_size, Fees(False), tick, Preferences()
    )
    assert sum(f.net for f in partial) == expected - 100


def test_first_target_batch_preserves_order_shared_depth_and_dust(monkeypatch):
    calls = []
    original = sorted

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(rules, "sorted", counted, raising=False)
    targets = [
        Target("late", 33_000, 5_000_000, 3),
        Target("dust-a", 30_000, 2_000_000, 2),
        Target("dust-b", 30_000, 3_000_000, 1),
    ]
    bids = [(35_000, 7_000_000), (34_000, 5_000_000), (10_000, 1_000_000)]
    all_batches = sell_batches(bids, targets, 5_000_000, Fees(False), tick=1_000)
    assert len(calls) == 2 and len(all_batches) == 2
    assert [t.id for t in all_batches[0].targets] == ["dust-b", "dust-a"]
    assert [(f.price, f.net) for f in all_batches[1].fills] == [
        (35_000, 2_000_000),
        (34_000, 3_000_000),
    ]
    calls.clear()
    assert (
        sell_batches(bids, targets, 5_000_000, Fees(False), tick=1_000, first_only=True)
        == all_batches[:1]
    )
    assert len(calls) == 2
    rng = Random(93)
    for _ in range(200):
        random_bids = [(p, rng.randrange(20_000_000)) for p in range(1_000, 100_001, 1_000)]
        random_targets = [
            Target(
                str(i), rng.randrange(1, 100) * 1_000, rng.randrange(10_000_000), rng.randrange(10)
            )
            for i in range(30)
        ]
        fee = Fees(True, 40_000, rng.randrange(3))
        assert (
            sell_batches(random_bids, random_targets, 5_000_000, fee, tick=1_000, first_only=True)
            == sell_batches(random_bids, random_targets, 5_000_000, fee, tick=1_000)[:1]
        )


def test_bid_content_hash_cache_preserves_stop_versions_and_storage(monkeypatch):
    hashes = []
    original = books.sha256

    def counted(data):
        hashes.append(data)
        return original(data)

    monkeypatch.setattr(books, "sha256", counted)
    b = Book()
    b.snapshot([(15_000, 10_000_000), (10_000, 5_000_000)], [(20_000, 20_000_000)], 100)
    expected = sha256(repr(sorted(b.bid.external.items())).encode()).hexdigest()
    assert b.bid.version == b.bid.version == expected
    assert len(hashes) == 1
    b.bid.consume(15_000, 1_000_000)
    b.snapshot([(10_000, 5_000_000), (15_000, 10_000_000)], [(20_000, 30_000_000)], 101)
    assert b.bid.version == expected and len(hashes) == 1
    b.delta("BID", [(15_000, 9_000_000)], 102)
    assert b.bid.version != expected and len(hashes) == 2
    b.disconnect()
    b.snapshot([(15_000, 9_000_000)], [], 1)
    assert b.bid.version == sha256(repr([(15_000, 9_000_000)]).encode()).hexdigest()
    assert len(hashes) == 3
    assert set(asdict(b.bid)) == {"external", "consumed"}
    assert set(asdict(b)) == {"bid", "ask", "ready", "timestamp", "revision"}


def test_progress_qualification_keeps_inclusive_boundary_and_close_precedence():
    c = Preferences()
    t = token(opened_ns=3, ends_ns=10 * D + 4)
    boundary = t.opened_ns + (t.ends_ns - t.opened_ns) * c.maxMarketProgressPercent // 100
    assert rules.static_reason(t, c, boundary, set()) is None
    assert rules.static_reason(t, c, boundary + 1, set()) == "PROGRESS_ABOVE_MAX"
    assert rules.static_reason(t, c, t.opened_ns - 1, set()) == "PROGRESS_BELOW_ZERO"
    c.maxMarketProgressPercent = 100
    assert rules.static_reason(t, c, t.ends_ns - 1, set()) is None
    assert rules.static_reason(t, c, t.ends_ns, set()) == "CLOSED"
    assert (
        rules.static_reason(replace(t, game_start_ns=boundary), c, boundary, set()) == "GAME_START"
    )
