import json
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.enums import LiquiditySide, OrderSide, OrderType
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    StrategyId,
    TradeId,
    TraderId,
    VenueOrderId,
)
from nautilus_trader.model.objects import Money

from pm_nautilus.books import Book, Side
from pm_nautilus.config import SCALE
from pm_nautilus.native import price, quantity
from pm_nautilus.rules import Fees, Token, cost
from pm_nautilus.store import Store

WEEK = int(datetime(2025, 12, 29, tzinfo=timezone.utc).timestamp()) * 10**9
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cohort_report.py"
spec = spec_from_file_location("cohort_report", SCRIPT)
reporter = module_from_spec(spec)
spec.loader.exec_module(reporter)
cohort_report = reporter.cohort_report
NS_PER_DAY = reporter.NS_PER_DAY


def _token(token_id, *, fees=Fees(False), open=True):
    return Token(
        token_id,
        f"event-{token_id}",
        f"market-{token_id}",
        f"condition-{token_id}",
        "Yes",
        2,
        False,
        WEEK - NS_PER_DAY,
        WEEK + 10 * NS_PER_DAY,
        fees,
        SCALE,
        1000,
        category_ids=("2", "1"),
        category_labels=("Politics", "World"),
        open=open,
    )


def _fill(
    store,
    token_id,
    oid,
    tid,
    side,
    q,
    p,
    ts,
    *,
    kind=None,
    fee=0,
    target=None,
    targets=None,
    legacy=False,
    has_intent=True,
):
    if has_intent:
        intent = {
            "event_id": f"event-{token_id}",
            "token_id": token_id,
            "side": side,
            "kind": kind,
            "generation": store.generation,
            "terminal": True,
        }
        if targets is not None:
            intent["targets"] = targets
        store.save_intent(oid, intent)
    info = (
        {}
        if legacy
        else {
            "pm_amount": cost(p, q) + (fee if side == "BUY" else -fee),
            "pm_fee": fee,
            "pm_generation": store.generation,
            "pm_kind": kind,
        }
    )
    if target is not None:
        info["pm_rules"] = {"target": target}
    return store.journal(
        OrderFilled(
            TraderId("TEST-001"),
            StrategyId("TEST-001"),
            InstrumentId.from_str(f"condition-{token_id}-{token_id}.POLYMARKET"),
            ClientOrderId(oid),
            VenueOrderId(f"venue-{oid}"),
            AccountId("POLYMARKET-TEST"),
            TradeId(tid),
            None,
            getattr(OrderSide, side),
            OrderType.LIMIT,
            quantity(q),
            price(p),
            pUSD,
            Money(Decimal(fee) / SCALE, pUSD),
            LiquiditySide.TAKER,
            UUID4(),
            ts,
            ts,
            info=info,
        )
    )


def _cohorts_by_price(report):
    return {row["buyPriceBandCents"]["lowerInclusive"]: row for row in report["cohorts"]}


def _cycle(token_id, shares, basis):
    return {"token_id": token_id, "quantity": shares * SCALE, "cost": basis}


def test_report_uses_committed_wal_exact_partial_target_fees_and_saved_depth(tmp_path):
    path = tmp_path / "ledger with spaces" / "state.sqlite"
    store = Store(path)
    try:
        store.save_token(_token("a"))
        store.save_token(_token("b", fees=Fees(True, 200_000, 0)))
        _fill(
            store,
            "a",
            "buy",
            "a1",
            "BUY",
            10 * SCALE,
            20_000,
            WEEK - 1,
            kind="BUY",
            fee=10_000,
            target=40_000,
        )
        _fill(
            store,
            "a",
            "buy",
            "a2",
            "BUY",
            10 * SCALE,
            30_000,
            WEEK,
            kind="BUY",
            fee=20_000,
            target=50_000,
        )
        _fill(
            store,
            "a",
            "sell",
            "s1",
            "SELL",
            4 * SCALE,
            50_000,
            WEEK + NS_PER_DAY,
            kind="TARGET",
            fee=3_000,
            targets=["buy:a1", "buy:a2"],
        )
        _fill(
            store,
            "a",
            "sell",
            "s2",
            "SELL",
            8 * SCALE,
            50_000,
            WEEK + 2 * NS_PER_DAY,
            kind="TARGET",
            fee=8_000,
            targets=["buy:a1", "buy:a2"],
        )
        _fill(
            store,
            "a",
            "settle",
            "z1",
            "SELL",
            8 * SCALE,
            0,
            WEEK + 3 * NS_PER_DAY,
            kind="SETTLEMENT",
        )
        _fill(
            store,
            "b",
            "hold",
            "b1",
            "BUY",
            5 * SCALE,
            400_000,
            WEEK,
            kind="BUY",
            fee=5_000,
            target=900_000,
        )
        cursor = _fill(store, "c", "claim", "c1", "BUY", 2 * SCALE, 100_000, WEEK, kind="BUY")
        _fill(store, "b", "pending", "p1", "BUY", 1 * SCALE, 400_000, WEEK, kind="BUY", fee=9_000)
        store.put(
            "business",
            Store.empty_business()
            | {
                "cursor": cursor,
                "realized": 59_000,
                "cycles": {"b": _cycle("b", 5, 2_005_000), "c": _cycle("c", 2, 200_000)},
                "claims": {"condition-c": {"token_id": "c", "state": "IDENTIFIED"}},
            },
        )
        store.save_book(
            "b",
            Book(
                bid=Side({800_000: 6 * SCALE}, {800_000: 2 * SCALE}),
                ready=True,
                timestamp=WEEK,
            ),
        )
        wal = path.with_name(path.name + "-wal")
        original = {p.name: p.read_bytes() for p in (path, wal)}
        report = cohort_report(path)
        assert {p.name: p.read_bytes() for p in (path, wal)} == original
        assert report["mode"] == "TEST" and report["unprojectedFillCount"] == 1
        assert report["applicationRealizedPnlU"] == "0.059"
        assert report["recordedBuyFills"] == {
            "fills": 4,
            "shares": "27",
            "cashAmountU": "2.735",
            "feesU": "0.035",
        }
        rows = _cohorts_by_price(report)
        a, b, held, pending = rows[2], rows[3], rows[40], rows[10]
        assert (a["buyWeekUtc"], b["buyWeekUtc"]) == ("2025-W52", "2026-W01")
        assert a["categoryIds"] == ["1", "2"] and a["buyFillCount"] == 1
        assert a["targetSellRate"] == "1.000000" and a["averageSellDays"] == "1.600000"
        assert (a["buyFeesU"], a["sellFeesU"], a["realizedPnlU"]) == ("0.01", "0.009", "0.281")
        assert b["targetSellRate"] == "0.200000" and b["averageSellDays"] == "2.000000"
        assert (
            b["zeroSettlementRate"] == "1.000000" and b["zeroSettlementShareOfBought"] == "0.800000"
        )
        assert (b["buyFeesU"], b["sellFeesU"], b["realizedPnlU"]) == ("0.02", "0.002", "-0.222")
        assert held["heldShares"] == "5" and held["heldBidCoveredShares"] == "4"
        assert held["heldBidValueU"] == "2.4" and held["heldUnpricedCostU"] == "0.401"
        assert (
            held["heldBidCoveredCostU"] == "1.604"
            and held["heldBidValueToCoveredCost"] == "1.496259"
        )
        assert pending["categoryIds"] == [] and pending["heldPendingSettlementShares"] == "2"
        assert pending["heldBidValueU"] is None and pending["heldUnpricedCostU"] == "0.2"
        assert report["coverage"]["oldestUsedBidSnapshotUtc"] == "2025-12-29T00:00:00+00:00"
    finally:
        store.close()

    files = sorted(path.parent.iterdir())
    assert files == [path]
    original = path.read_bytes()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == report
    assert sorted(path.parent.iterdir()) == files and path.read_bytes() == original


def test_report_handles_legacy_storage_and_marks_inferred_lot_allocation(tmp_path):
    path = tmp_path / "legacy.sqlite"
    store = Store(path, mode="LIVE")
    try:
        token = _token("a")
        store.db.execute(
            "INSERT INTO tokens VALUES (?,?)", (token.token_id, json.dumps(asdict(token)))
        )
        _fill(store, "a", "buy1", "a1", "BUY", 5 * SCALE, 20_000, WEEK, legacy=True, fee=1_000)
        _fill(store, "a", "buy2", "a2", "BUY", 3 * SCALE, 30_000, WEEK, legacy=True, fee=1_000)
        _fill(
            store,
            "a",
            "stop1",
            "s1",
            "SELL",
            4 * SCALE,
            10_000,
            WEEK + NS_PER_DAY,
            kind="STOP",
            legacy=True,
            fee=2_000,
        )
        cursor = _fill(
            store,
            "a",
            "stop2",
            "s2",
            "SELL",
            4 * SCALE,
            10_000,
            WEEK + 2 * NS_PER_DAY,
            kind="STOP",
            legacy=True,
            fee=2_000,
        )
        store.db.execute("DROP TABLE business_parts")
        store._put_meta(
            "business", Store.empty_business() | {"cursor": cursor, "realized": -116_000}
        )
    finally:
        store.close()
    report = cohort_report(path)
    totals = report["totals"]
    assert report["mode"] == "LIVE"
    assert totals["boughtShares"] == totals["estimatedExitAllocationShares"] == "8"
    assert totals["averageSellDays"] is None and totals["sellAgeCoveredShares"] == "0"
    assert totals["buyFeesU"] == "0.002" and totals["sellFeesU"] == "0.004"
    assert totals["realizedPnlU"] == "-0.116" and totals["zeroSettlementRate"] is None
    assert totals["knownFrozenTargetBuyShares"] == "0"


def test_report_does_not_invent_target_links_or_allocate_unknown_ownership(tmp_path):
    path = tmp_path / "unmatched.sqlite"
    store = Store(path)
    try:
        _fill(store, "a", "buy", "a1", "BUY", 5 * SCALE, 20_000, WEEK, kind="BUY", target=40_000)
        _fill(
            store,
            "a",
            "unknown-sell",
            "s1",
            "SELL",
            SCALE,
            50_000,
            WEEK + NS_PER_DAY,
            kind="TARGET",
            targets=["not-stored:trade", "buy:a1"],
            fee=1_000,
        )
        cursor = _fill(
            store,
            "b",
            "no-intent",
            "u1",
            "BUY",
            SCALE,
            30_000,
            WEEK,
            kind="BUY",
            fee=2_000,
            has_intent=False,
        )
        store.put(
            "business",
            Store.empty_business()
            | {
                "cursor": cursor,
                "cycles": {"a": _cycle("a", 4, 80_000), "b": _cycle("b", 1, 32_000)},
            },
        )
    finally:
        store.close()
    report = cohort_report(path)
    assert report["coverage"]["unattributedBuys"]["feesU"] == "0.002"
    assert report["coverage"]["unattributedSells"]["feesU"] == "0.001"
    assert report["coverage"]["unreconciledSavedHeldShares"] == "5"
    assert report["totals"]["verifiedTargetSellShares"] == "0"
    assert report["totals"]["heldBidValueU"] is None
    assert report["totals"]["unreconciledOpenLotShares"] == "5"
