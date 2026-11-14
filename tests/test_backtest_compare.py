"""Check the actual SQLite export and comparison data path."""

from copy import deepcopy

import pytest
from nautilus_trader.common.component import TestClock as ReplayClock

from pm_nautilus.backtest_compare import compare, export_observed, COMPARISON_SCHEMA
from pm_nautilus.performance import PerformanceSampler
from pm_nautilus.test_runtime import create_runtime

from test_backtest_engine import END, NS_PER_MS, START, book, header, market, replay, tokens


def test_database_export_compares_unaggregated_fills_and_respects_time_window(tmp_path):
    path = tmp_path / "backup.sqlite"
    clock = ReplayClock()
    clock.set_time(START * NS_PER_MS)
    runtime = create_runtime(path, clock=clock)
    try:
        runtime.update_preferences({}, capital="1")
        runtime.start()
        runtime.add_tokens(tokens())
        runtime.book("1", [(15_000, 100_000_000)], [(20_000, 25_000_000), (21_000, 30_000_000)])
        runtime.book("2", [(970_000, 100_000_000)], [(980_000, 50_000_000)])
        PerformanceSampler(runtime).sample(START)
        clock.set_time((END + 1) * NS_PER_MS)
        runtime.book("1", [(450_000, 100_000_000)], [(460_000, 100_000_000)])
    finally:
        runtime.close()
    original = path.read_bytes()
    observed = export_observed(path, START, END)
    assert observed["schema"] == COMPARISON_SCHEMA
    assert [row["price"] for row in observed["fills"]] == ["0.020000", "0.021000"]
    assert path.read_bytes() == original
    result = replay(
        header(),
        market(),
        book("1") | {"asks": [[20_000, 25_000_000], [21_000, 30_000_000]]},
        book("2", bid=970_000, ask=980_000),
    )
    comparison = compare(result, observed)
    assert comparison["fullFillLists"]
    assert comparison["pairedMatches"] == comparison["exactMatches"] == 2
    assert comparison["unmatchedObserved"] == comparison["unmatchedReplayed"] == 0
    assert comparison["equityComparison"]["pairedValuedSamples"] == 1
    assert comparison["equityComparison"]["meanAbsoluteTotalDelta"] == "0"
    assert comparison["equityComparison"]["replaySamplesWithoutNearbyObservation"] > 0


def test_pairings_are_one_to_one_and_report_each_mismatch_and_truncation():
    row = {
        "atMs": START + 100,
        "tokenId": "1",
        "side": "BUY",
        "kind": "BUY",
        "price": "0.02",
        "quantity": "10",
        "fee": "0",
        "amount": "0.2",
    }
    observed = {
        "schema": COMPARISON_SCHEMA,
        "startMs": START,
        "endMs": END,
        "fills": [row, row | {"atMs": START + 1_000}, row | {"tokenId": "2"}],
        "summary": {"fillCount": 3, "fillsTruncated": False},
    }
    result = {
        "status": "completed",
        "startMs": START,
        "endMs": END,
        "fills": [row, row | {"price": "0.021", "atMs": START + 1_002}, row | {"tokenId": "3"}],
        "summary": {"fillCount": 4, "fillsTruncated": True},
    }
    report = compare({"result": result}, observed, tolerance_ms=10)
    assert report["exactMatches"] == report["pairedWithinPriceAndQuantityTolerance"] == 1
    assert report["pairedMatches"] == 2
    assert report["unmatchedObserved"] == report["unmatchedReplayed"] == 1
    assert report["matchedDetails"][0]["atDeltaMs"] == 2
    assert report["matchedDetails"][0]["priceDelta"] == "0.001"
    assert not report["fullFillLists"] and report["replayOmittedCount"] == 1
    adjusted = compare(result, observed, tolerance_ms=10, tolerance_price="0.001")
    assert adjusted["pairedWithinPriceAndQuantityTolerance"] == 2
    bad = deepcopy(observed)
    del bad["schema"]
    with pytest.raises(ValueError, match="逐笔成交"):
        compare(result, bad)


def test_equity_comparison_never_values_gaps_or_interpolates():
    actual = {
        "schema": COMPARISON_SCHEMA,
        "startMs": START,
        "endMs": END,
        "fills": [],
        "summary": {"fillCount": 0, "fillsTruncated": False},
        "equity": [
            {"atMs": START + 3_000, "total": "4", "pnl": "3"},
            {"atMs": START + 62_000, "total": None, "pnl": None},
        ],
    }
    replay = {
        "status": "completed",
        "startMs": START,
        "endMs": END,
        "fills": [],
        "summary": {"fillCount": 0, "fillsTruncated": False},
        "equity": [
            {"atMs": START, "total": "5", "pnl": "4"},
            {"atMs": START + 60_000, "total": "6", "pnl": "5"},
            {"atMs": START + 120_000, "total": "7", "pnl": "6"},
        ],
    }
    matched = compare(replay, actual)["equityComparison"]
    assert matched["pairedValuedSamples"] == 1
    assert matched["pairedUnpricedSamples"] == 1
    assert matched["replaySamplesWithoutNearbyObservation"] == 1
    assert matched["meanAbsoluteTotalDelta"] == "1"
    assert matched["maxObservedMatchSkewMs"] == 3_000
