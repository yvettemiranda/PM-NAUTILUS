"""Partial UI estimates never alter strict analytics or owned financial rights."""

from copy import deepcopy
from decimal import Decimal

from pm_nautilus.config import SCALE, micros, units
from pm_nautilus.performance import PerformanceSampler
from pm_nautilus.redemption import RedemptionService
from pm_nautilus.views import dashboard, portfolio_view, records
from test_market_availability_views import owned_positions, unavailable
from test_runtime import setup


def cycle_order(runtime):
    return [cycle["token_id"] for cycle in runtime.business["cycles"].values()]


def mark_unavailable(runtime, token_id):
    runtime.market_availability[token_id] = unavailable(runtime.tokens[token_id])


def test_stable_full_partition_precedes_twenty_position_preview_and_all_totals(
    tmp_path, monkeypatch
):
    import pm_nautilus.views as views

    r, _ = owned_positions(tmp_path, 23)
    original = cycle_order(r)
    missing = {original[0], original[5], original[-1]}
    mark_unavailable(r, original[0])
    r.books[original[5]].ready = False
    mark_unavailable(r, original[-1])
    # A ready no-bid book is valued at a known zero; it stays in the normal group.
    r.books[original[1]].snapshot([], [], r.now)
    expected = [tid for tid in original if tid not in missing] + [
        tid for tid in original if tid in missing
    ]
    before = deepcopy(r.business)
    changes = r.store.db.total_changes
    calls = []
    project = views.token_view

    def counted(runtime, token):
        calls.append(token.token_id)
        return project(runtime, token)

    monkeypatch.setattr(views, "token_view", counted)
    try:
        full, totals = portfolio_view(r)
        assert [row["tokenId"] for row in full] == expected
        assert full[20]["tokenId"] == original[0]
        assert full[20]["currentSellPriceStatus"] == "UNAVAILABLE"
        assert [row["tokenId"] for row in full[-3:]] == [tid for tid in original if tid in missing]
        calls.clear()
        preview, preview_totals = portfolio_view(r, 20)
        assert calls == expected[:20]  # No expensive row projection for hidden positions.
        assert [row["tokenId"] for row in preview] == expected[:20]
        assert all(row["currentSellPriceStatus"] in {"READY", "NO_BID"} for row in preview)
        calls.clear()
        assert portfolio_view(r, 0) == ([], totals)
        assert not calls and preview_totals == totals
        for limit in (0, 20, None):
            view = dashboard(r, limit=0, position_limit=limit)
            assert view["portfolio"] == totals and view["positionCount"] == 23
            assert view["valuedPositionCount"] == 20 and view["unvaluedPositionCount"] == 3
        assert totals["knownPositionValue"] == "14.25"
        assert totals["unpricedPositionCost"] == "3"
        assert totals["estimatedPositionValue"] == "17.25"
        assert totals["estimatedTotalFunds"] == "94.25"
        assert totals["knownUnrealizedPnl"] == totals["knownTotalPnl"] == "-5.75"
        assert totals["totalFunds"] is totals["positionValue"] is totals["unrealizedPnl"] is None
        assert all(row["remainingCost"] == "1" for row in full)
        assert all(row["unrealizedPnl"] is None for row in full[-3:])
        assert all(row["estimatedUnrealizedPnl"] == "0" for row in full[-3:])
        zero = next(row for row in full if row["tokenId"] == original[1])
        assert zero["executableSellValue"] == "0" and zero["unrealizedPnl"] == "-1"
        assert zero["estimatedUnrealizedPnl"] == "-1"
        assert r.business == before and r.store.db.total_changes == changes
        # Recovery returns the healthy position to its original relative order.
        r.market_availability.clear()
        r.books[original[5]].ready = True
        restored, healthy_totals = portfolio_view(r)
        assert [row["tokenId"] for row in restored] == original
        assert healthy_totals["totalFunds"] == healthy_totals["estimatedTotalFunds"]
        assert healthy_totals["positionValue"] == healthy_totals["estimatedPositionValue"]
        assert healthy_totals["unrealizedPnl"] == healthy_totals["knownUnrealizedPnl"]
        assert all(row["estimatedUnrealizedPnl"] == row["unrealizedPnl"] for row in restored)
    finally:
        r.close()


def test_partial_sale_keeps_exact_remaining_cost_and_already_realized_profit(tmp_path):
    r, _, token = setup(tmp_path, fees=True)
    r.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    r.start()
    r.pause()
    # Real native TEST fills allocate the historical fee-inclusive basis using
    # integer arithmetic. Remaining cost cannot be reconstructed from rounded avg.
    r.book("1", [(35_000, 7_000_000)], [(20_000, 50_000_000)])
    cycle = r.business["cycles"][token.event_id]
    assert cycle["quantity"] == 41_000_000 and cycle["cost"] == 852_143
    realized = r.business["realized"]
    assert realized > 0
    expected_history = records(r, 20)
    mark_unavailable(r, token.token_id)
    before = deepcopy(r.business)
    changes = r.store.db.total_changes
    try:
        positions, total = portfolio_view(r)
        row = positions[0]
        assert row["remainingCost"] == "0.852143"
        assert micros(row["averageBuyPrice"]) * 41 != cycle["cost"]
        assert row["unrealizedPnl"] is None and row["executableSellValue"] is None
        assert row["estimatedUnrealizedPnl"] == "0"
        assert total["unpricedPositionCost"] == total["estimatedPositionValue"] == "0.852143"
        assert total["knownPositionValue"] == total["knownUnrealizedPnl"] == "0"
        assert total["knownTotalPnl"] == total["realizedPnl"] == units(realized)
        assert total["estimatedTotalFunds"] == units(r.cash() + cycle["cost"])
        assert total["totalFunds"] is total["unrealizedPnl"] is None
        assert records(r, 20) == expected_history
        assert r.business == before and r.store.db.total_changes == changes
        assert r.validate()["ok"]
    finally:
        r.close()


def test_not_ready_position_cost_is_estimated_but_zero_bid_value_is_known(tmp_path):
    r, _ = owned_positions(tmp_path, 2)
    original = cycle_order(r)
    r.books[original[0]].ready = False
    r.books[original[1]].snapshot([], [], r.now)
    try:
        positions, total = portfolio_view(r)
        assert [row["currentSellPriceStatus"] for row in positions] == ["NO_BID", "NOT_READY"]
        assert positions[0]["executableSellValue"] == "0"
        assert positions[0]["unrealizedPnl"] == "-1"
        assert positions[0]["estimatedUnrealizedPnl"] == "-1"
        assert positions[1]["executableSellValue"] is None
        assert positions[1]["unrealizedPnl"] is None
        assert positions[1]["estimatedUnrealizedPnl"] == "0"
        assert total["unpricedPositionCost"] == total["estimatedPositionValue"] == "1"
        assert total["estimatedTotalFunds"] == "99"
        assert total["knownPositionValue"] == "0"
        assert total["knownUnrealizedPnl"] == total["knownTotalPnl"] == "-1"
        assert total["notReadyPositionCount"] == total["valuedPositionCount"] == 1
    finally:
        r.close()


def test_failed_claim_cost_is_separate_from_receivable_and_known_pnl(tmp_path, monkeypatch):
    r, tokens = owned_positions(tmp_path, 4)
    service = RedemptionService(r, None)
    # Use genuine owned cycles and the existing claim identification path;
    # views must retain every right, settled marker, target and journal row.
    for token, payout in zip(tokens, [SCALE, SCALE // 2, 0, SCALE], strict=True):
        service.identify(token, {token.token_id: payout, "counter": SCALE - payout})
    claims = [r.business["claims"][token.condition_id] for token in tokens]
    claims[0]["state"] = "IDENTIFIED"  # 50 receivable, 1 cost.
    claims[1].update(state="CONFIRMED", cash_included=True)  # 25 already in cash.
    claims[2]["state"] = "FAILED"  # Unknown right at original cost; not zero-loss proof.
    claims[3].update(state="FAILED", cash_included=True)  # Never double add observed cash.
    assert all(type(claim["cost"]) is int and claim["cost"] >= 0 for claim in claims)
    # Model the account reader after its existing LIVE balance refresh, without
    # manufacturing any native fill, receipt, ledger credit or business mutation.
    monkeypatch.setattr(r, "cash", lambda: 171_000_000)
    before = deepcopy(r.business)
    changes = r.store.db.total_changes
    try:
        for limit in (0, 20, None):
            rows, total = portfolio_view(r, limit)
            assert not rows
            assert total["estimatedPositionValue"] == total["unpricedPositionCost"] == "0"
            assert total["pendingRedemption"] == "50"
            assert total["failedRedemption"] == "50"  # Diagnostic amount, never an addend.
            assert total["failedRedemptionCost"] == "2"
            assert total["failedRedemptionCostFallback"] == "1"
            assert total["failedRedemptionCount"] == 2
            assert total["failedRedemptionCashIncludedCount"] == 1
            assert total["estimatedTotalFunds"] == "222"  # 171 cash + 50 pending + 1 cost.
            assert total["knownUnrealizedPnl"] == total["knownTotalPnl"] == "73"
            assert total["positionValue"] == "0"
            assert total["totalFunds"] is total["unrealizedPnl"] is None
        assert r.business == before and r.store.db.total_changes == changes
    finally:
        r.close()


def test_reserved_cash_is_included_once_in_estimate(tmp_path):
    r, _ = owned_positions(tmp_path)
    tid = cycle_order(r)[0]
    mark_unavailable(r, tid)
    r.store.save_intent(
        "reserved",
        {
            "event_id": "another-event",
            "token_id": tid,
            "side": "BUY",
            "cash": 2_000_000,
            "spent": 500_000,
            "terminal": False,
        },
    )
    try:
        _, total = portfolio_view(r, 0)
        assert total["availableCash"] == "97.5" and total["reservedCash"] == "1.5"
        assert total["estimatedTotalFunds"] == "100"
        assert Decimal(total["estimatedTotalFunds"]) == (
            Decimal(total["availableCash"])
            + Decimal(total["reservedCash"])
            + Decimal(total["estimatedPositionValue"])
        )
    finally:
        r.close()


def test_cost_estimate_does_not_backfill_strict_performance_history(tmp_path):
    r, _ = owned_positions(tmp_path)
    sampler = PerformanceSampler(r)
    sampler.sample(1_000)
    historical = tuple(r.store.db.execute("SELECT * FROM equity_samples").fetchone())
    mark_unavailable(r, cycle_order(r)[0])
    try:
        _, display = portfolio_view(r, 0)
        assert display["estimatedTotalFunds"] == "100"
        sampler.sample(61_000)
        samples = r.store.db.execute("SELECT * FROM equity_samples ORDER BY id").fetchall()
        assert tuple(samples[0]) == historical
        assert samples[1]["pnl"] is None and samples[1]["total"] is None
        assert samples[0]["pnl"] is not None and samples[0]["total"] is not None
    finally:
        r.close()
