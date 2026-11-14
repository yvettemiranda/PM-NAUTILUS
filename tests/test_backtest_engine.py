import hashlib
import json
from dataclasses import asdict

import pytest
from nautilus_trader.common.component import TestClock as ReplayClock

from pm_nautilus.backtest_engine import NS_PER_MS, run_backtest
from pm_nautilus.rules import Fees, Token
from pm_nautilus.test_runtime import create_runtime


START = 1_790_000_000_000
DAY_NS = 86_400_000_000_000
END = START + 180_000


def tokens():
    return [
        Token(
            str(i),
            "event",
            "market",
            "condition",
            label,
            2,
            False,
            START * NS_PER_MS - DAY_NS,
            START * NS_PER_MS + 9 * DAY_NS,
            Fees(False),
            5_000_000,
            1_000,
        )
        for i, label in [(1, "Yes"), (2, "No")]
    ]


def header(start=START, end=END):
    return {
        "type": "header",
        "version": 1,
        "source": "archived fixture",
        "coverage": "scoped",
        "startMs": start,
        "endMs": end,
    }


def market(at=START):
    return {"type": "market", "atMs": at, "tokens": [asdict(token) for token in tokens()]}


def book(token, at=START, bid=15_000, ask=20_000):
    return {
        "type": "book",
        "atMs": at,
        "tokenId": token,
        "bids": [[bid, 100_000_000]],
        "asks": [[ask, 50_000_000]],
    }


def delta(token, elapsed, price, quantity):
    return {
        "type": "delta",
        "atMs": START + elapsed,
        "tokenId": token,
        "side": "BUY",
        "levels": [[price, quantity]],
    }


def settlement(elapsed):
    return {
        "type": "settlement",
        "atMs": START + elapsed,
        "conditionId": "condition",
        "payouts": {"1": 1_000_000, "2": 0},
    }


def replay(*rows, **kwargs):
    request = dict(preferences={}, starting_cash="1") | kwargs
    return run_backtest(map(json.dumps, rows), **request)


def test_historical_depth_replay_reuses_fill_rules_and_shadows_consumed_depth():
    rows = (
        header(),
        market(),
        book("1"),
        book("2", bid=970_000, ask=980_000),
        delta("1", 1_000, 450_000, 27_000_000),
        delta("1", 10_000, 450_000, 27_000_000),
        delta("1", 20_000, 450_000, 67_000_000),
    )
    result = replay(*rows)
    assert [(r["atMs"], r["side"], r["price"], r["quantity"]) for r in result["fills"]] == [
        (START, "BUY", "0.02", "50"),
        (START + 1_000, "SELL", "0.45", "27"),
        (START + 20_000, "SELL", "0.45", "23"),
    ]
    assert result["summary"]["endingCash"] == "22.5"
    assert result["coverage"]["status"] == "scoped"
    assert result["coverage"]["bookTokenCount"] == 2
    assert replay(*rows) == result
    tighter = replay(*rows, preferences={"maxBuyPriceCents": "1.9"})
    assert tighter["fills"] == []
    assert tighter["summary"]["endingCash"] == "1"


def test_gap_marks_unpriced_points_and_requires_new_snapshot():
    rows = (
        header(),
        market(),
        book("1"),
        book("2", bid=970_000, ask=980_000),
        {"type": "gap", "atMs": START + 1_000, "tokenIds": ["1"]},
        delta("1", 2_000, 450_000, 27_000_000),
        book("1", START + 61_000, bid=450_000, ask=500_000),
    )
    result = replay(*rows)
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["ignoredDeltaCount"] == 1
    point = next(item for item in result["equity"] if item["atMs"] == START + 60_000)
    assert point["total"] is None and point["missingMarkCount"] == 1
    assert result["fills"][-1]["atMs"] == START + 61_000
    assert result["fills"][-1]["side"] == "SELL"


def test_settlement_is_observed_at_time_and_only_then_credits_after_control_tick():
    result = replay(
        header(), market(), book("1"), book("2", bid=970_000, ask=980_000), settlement(60_001)
    )
    assert result["coverage"]["status"] == "scoped"
    assert result["fills"][-1]["kind"] == "SETTLEMENT"
    assert result["fills"][-1]["atMs"] == START + 65_000
    assert result["fills"][-1]["quantity"] == "50"
    assert result["summary"]["endingCash"] == "50"
    assert result["equity"][1]["cash"] == "0"


def test_warmup_does_not_buy_before_start_and_drops_late_records():
    result = replay(
        header(START - 120_000),
        market(START - 120_000),
        book("1", START - 119_000),
        book("2", START - 119_000, bid=970_000, ask=980_000),
        delta("1", 20_000, 450_000, 100_000_000),
        start_ms=START,
        end_ms=START + 10_000,
    )
    assert result["startMs"] == START
    assert result["summary"]["fillCount"] == 1
    assert result["fills"][0]["atMs"] == START


def test_bad_observation_order_and_missing_scope_do_not_produce_a_curve():
    with pytest.raises(ValueError, match="第 4 行.*观测时间"):
        replay(header(), market(), book("1", START + 1), book("2"))
    with pytest.raises(ValueError, match="没有可重建的历史盘口"):
        replay(header(), market())
    with pytest.raises(ValueError, match="合约"):
        replay(header(), book("1"), market())


def test_checkpoint_replays_open_position_in_scratch_and_rejects_lookahead(tmp_path):
    ledger = tmp_path / "exported-state.sqlite"
    clock = ReplayClock()
    clock.set_time((START - 30_000) * NS_PER_MS)
    runtime = create_runtime(ledger, clock=clock)
    try:
        runtime.update_preferences({}, capital="1")
        runtime.add_tokens(tokens())
        runtime.start()
        for tid, bid, ask in [("1", 15_000, 20_000), ("2", 970_000, 980_000)]:
            runtime.book(tid, [(bid, 100_000_000)], [(ask, 50_000_000)])
        assert runtime.cash() == 0
    finally:
        runtime.close()
    initial_sha = hashlib.sha256(ledger.read_bytes()).digest()
    report = replay(
        header(),
        book("1", bid=450_000, ask=460_000),
        book("2", bid=530_000, ask=540_000),
        checkpoint=ledger,
        checkpoint_at_ms=START,
        workspace=tmp_path,
    )
    assert report["coverage"]["checkpointAtMs"] == START
    assert report["coverage"]["emptyStartingPositions"] is False
    assert report["summary"]["startingPositionCount"] == 1
    assert report["startingCash"] == "0"
    assert [f["side"] for f in report["fills"]] == ["SELL"]
    assert report["summary"]["endingCash"] == "22.5"
    assert hashlib.sha256(ledger.read_bytes()).digest() == initial_sha
    assert list(tmp_path.iterdir()) == [ledger]
    earlier = START - 60_000
    with pytest.raises(ValueError, match="晚于回测起点"):
        replay(header(earlier), checkpoint=ledger, checkpoint_at_ms=earlier)
    with pytest.raises(ValueError, match="原始参数"):
        replay(
            header(),
            checkpoint=ledger,
            checkpoint_at_ms=START,
            preferences={"maxBuyPriceCents": "1.9"},
        )
