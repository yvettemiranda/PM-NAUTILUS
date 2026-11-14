import asyncio
import gzip
import json
from io import BytesIO

import pytest
from test_backtest_engine import (
    END,
    NS_PER_MS,
    START,
    ReplayClock,
    book,
    delta,
    header,
    market,
    replay,
    tokens,
)

from pm_nautilus.backtest_engine import run_backtest
from pm_nautilus.capture import CaptureRecorder
from pm_nautilus.capture_replay import inspect_capture, main, open_capture_tapes, open_replay_input
from pm_nautilus.config import Preferences
from pm_nautilus.market import MarketService
from pm_nautilus.test_runtime import create_runtime

SCOPE = {"maxBuyPriceMicros": 50_000, "marketCoverage": "test-subscribed"}
PREFS = {"maxBuyPriceCents": "5"}


def scope(at=START, **preferences):
    return {
        "type": "scope",
        "atMs": at,
        "captureScope": SCOPE | {"preferences": Preferences(**preferences).model_dump(mode="json")},
        "baseline": True,
    }


def control(at=START, status="RUNNING", baseline=True):
    return {"type": "control", "atMs": at, "status": status, "baselineEnd": baseline}


def filtered():
    one = book("1")
    return [
        market() | {"drain": False},
        {
            "type": "book_batch",
            "atMs": START,
            "drain": False,
            "books": [
                one,
                book("2") | {"bids": [], "asks": [], "bidCovered": False},
            ],
        },
    ]


def sealed(path, start, end, events, previous=None, suffix="0123456789ab"):
    identity = f"{start:013d}-{end:013d}-{suffix}.ndjson.gz"
    data = [
        {
            "type": "capture_segment",
            "version": 1,
            "startMs": start,
            "previous": previous,
            "captureScope": SCOPE,
        },
        *events,
        {"type": "capture_end", "endMs": end, "id": identity},
    ]
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for row in data:
            stream.write(json.dumps(row) + "\n")
    return identity


def test_capture_stitches_late_baseline_and_records_price_improvement(tmp_path):
    first, second = (tmp_path / name for name in ("renamed-first.gz", "renamed-second.gz"))
    baseline = [scope(), *filtered(), control()]
    first_identity = sealed(first, START, START + 1000, baseline)
    sealed(
        second, START + 2000, END, [delta("1", 3_000, 450_000, 50_000_000)], previous=first_identity
    )
    assert inspect_capture(first)["baselineAtMs"] == START
    assert inspect_capture(second)["baselineAtMs"] is None
    assert inspect_capture(first)["id"] == first_identity
    with open_capture_tapes([second, first]) as tape:
        result = run_backtest(tape, preferences=PREFS, starting_cash="1")
    assert [(row["side"], row["price"]) for row in result["fills"]] == [
        ("BUY", "0.02"),
        ("SELL", "0.45"),
    ]
    assert result["summary"]["realizedPnl"] == "21.5"
    assert result["coverage"]["explicitGapCount"] == 1
    with open_replay_input(first) as tape:
        assert run_backtest(tape, preferences=PREFS, starting_cash="1")["summary"]["fillCount"] == 1


def test_capture_rejects_more_than_five_cents_and_market_scope_widening():
    header_row = header() | {"captureScope": SCOPE}
    rows = [header_row, scope(minMarketDurationDays=2), *filtered(), control()]
    with pytest.raises(ValueError, match="最高 5¢"):
        replay(*rows, preferences={"maxBuyPriceCents": "5.1"})
    with pytest.raises(ValueError, match="候选范围"):
        replay(*rows, preferences=PREFS)


def test_capture_promotes_an_expensive_sibling_with_complete_bids_before_dispatch():
    baseline = filtered()
    baseline[-1]["books"][0].update(invalidGrid=True)
    promotion = {
        "type": "delta_batch",
        "atMs": START + 2_000,
        "changes": [{"tokenId": "1", "side": "SELL", "levels": [], "invalidGrid": True}],
        "books": [book("2", bid=10_000, ask=20_000)],
    }
    result = replay(
        header() | {"captureScope": SCOPE},
        scope(),
        *baseline,
        control(),
        promotion,
        delta("2", 3_000, 450_000, 50_000_000),
        preferences=PREFS,
    )
    assert [(row["tokenId"], row["side"], row["price"]) for row in result["fills"]] == [
        ("2", "BUY", "0.02"),
        ("2", "SELL", "0.45"),
    ]


def test_capture_gap_skips_next_partial_clip_and_marks_unpriced(tmp_path):
    first, second = [tmp_path / name for name in ("test1.gz", "test2.gz")]
    baseline = [scope(), *filtered(), control()]
    sealed(first, START, START + 1000, baseline)
    sealed(
        second,
        START + 70_000,
        END,
        [delta("1", 71_000, 450_000, 50_000_000)],
        previous=f"{START:013d}-{START + 2000:013d}-0123456789ab.ndjson.gz",
    )
    with open_capture_tapes([first, second]) as tape:
        result = run_backtest(tape, preferences=PREFS, starting_cash="1")
    assert [row["side"] for row in result["fills"]] == ["BUY"]
    assert result["summary"]["endingEquity"] is None
    assert result["coverage"]["explicitGapCount"] == 2


def test_capture_rejects_empty_or_corrupt_gzip_footer(tmp_path):
    target = tmp_path / "broken.gz"
    sealed(target, START, END, [scope(), control()])
    target.write_bytes(target.read_bytes()[:-7])
    with pytest.raises(EOFError):
        inspect_capture(target)


def test_capture_directory_and_atomic_gzip_export(tmp_path):
    baseline = [scope(), *filtered(), control()]
    identity = f"{START:013d}-{END:013d}-0123456789ab.ndjson.gz"
    sealed(tmp_path / identity, START, END, baseline)
    output = tmp_path / "export.ndjson.gz"
    main([str(output), "--directory", str(tmp_path)])
    with open_replay_input(tmp_path) as direct, open_replay_input(output) as stitched:
        assert list(direct) == list(stitched)
    saved = output.read_bytes()
    with pytest.raises(SystemExit):
        main([str(output), "--directory", str(tmp_path)])
    assert output.read_bytes() == saved
    assert list(tmp_path.glob(".capture-export-*")) == []


def test_capture_ignores_unsampled_market_after_it_leaves_subscription():
    rows = [
        header() | {"captureScope": SCOPE},
        scope(),
        *filtered(),
        control(),
        {"type": "gap", "atMs": START + 1000, "tokenIds": ["2", "1"], "detached": True},
    ]
    result = replay(*rows, preferences=PREFS)
    assert result["coverage"]["tokenCount"] == 1
    assert result["summary"]["endingCash"] == "0"
    assert result["equity"][-1]["total"] is None

    rows.extend([row | {"atMs": START + 2000} for row in [scope(), *filtered(), control()]])
    rows.append(delta("1", 3000, 450_000, 50_000_000))
    result = replay(*rows, preferences=PREFS)
    assert result["summary"]["endingCash"] == "22.5"


def test_gzip_replay_limits_read_size_before_decoding(monkeypatch):
    from pm_nautilus import backtest_engine

    monkeypatch.setattr(backtest_engine, "MAX_LINE_BYTES", 128)
    with gzip.GzipFile(fileobj=BytesIO(gzip.compress(b"x" * 10_000))) as raw:
        with pytest.raises(ValueError, match="单行回测数据超长"):
            run_backtest(raw, preferences=PREFS, starting_cash="1")
        assert raw.tell() == 129


def test_replay_verifies_touched_segment_even_when_selection_ends_earlier(tmp_path):
    identity = f"{START:013d}-{END:013d}-0123456789ab.ndjson.gz"
    target = tmp_path / identity
    sealed(target, START, END, [scope(), *filtered(), control(), delta("1", 3000, 450_000, 1)])
    target.write_bytes(target.read_bytes()[:-7])
    with open_replay_input(target, end_ms=START + 2_000) as tape:
        with pytest.raises(EOFError):
            run_backtest(tape, preferences=PREFS, starting_cash="1", end_ms=START + 2_000)


@pytest.mark.parametrize("mixed", [False, True])
def test_compact_touch_preserves_alternate_candidates_after_cash_releases(mixed):
    baseline = filtered()
    sibling_market = [
        item
        | {
            "token_id": str(index),
            "event_id": "event-b",
            "market_id": "market-b",
            "condition_id": "condition-b",
        }
        for index, item in enumerate(baseline[0]["tokens"], start=3)
    ]
    baseline[0]["tokens"].extend(sibling_market)
    baseline[0]["tokenRefs"] = [1, 2, 3, 4]
    baseline[1]["books"].extend(
        [book("3"), book("4") | {"bids": [], "asks": [], "bidCovered": False}]
    )
    at = START + 1100
    expanded = {"tokenId": "4", "side": "SELL", "levels": [], "invalidGrid": False}
    if mixed:
        meaningful = {"tokenId": "1", "side": "BUY", "levels": [[450_000, 1]]}
        legacy_row = {"type": "delta_batch", "atMs": at, "changes": [meaningful, expanded]}
        compact_row = {"type": "delta_batch", "atMs": at, "changes": [meaningful], "touches": [4]}
    else:
        legacy_row = {"type": "delta_batch", "atMs": at, "changes": [expanded]}
        compact_row = {"type": "touch", "atMs": at, "tokenRefs": [4]}
    observed = scope()
    observed["captureScope"]["candidateBatchNs"] = 1_000_000_000
    prefix = [
        header() | {"captureScope": SCOPE},
        observed,
        *baseline,
        control(),
        delta("1", 500, 450_000, 50_000_000),
    ]
    older = replay(*prefix, legacy_row, preferences=PREFS)
    compact = replay(*prefix, compact_row, preferences=PREFS)
    assert compact["fills"] == older["fills"]
    assert compact["equity"] == older["equity"]
    assert [(r["atMs"], r["side"], r["marketId"]) for r in compact["fills"]] == [
        (START, "BUY", "market"),
        (START + 500, "SELL", "market"),
        (at, "BUY", "market-b"),
    ]


def test_touch_rejects_unknown_or_conflicting_alias():
    baseline = filtered()
    baseline[0]["tokenRefs"] = [1, 1]
    rows = [header() | {"captureScope": SCOPE}, scope(), *baseline, control()]
    with pytest.raises(ValueError, match="合约编号发生改变"):
        replay(*rows, preferences=PREFS)
    baseline[0]["tokenRefs"] = [1, 2]
    rows.append({"type": "touch", "atMs": START + 1, "tokenRefs": [3]})
    with pytest.raises(ValueError, match="引用了未知合约"):
        replay(*rows, preferences=PREFS)


def test_replay_real_capture_writer_export_matches_test_ledger(tmp_path, monkeypatch):
    directory = tmp_path / "captures"

    async def record():
        clock = ReplayClock()
        clock.set_time(START * NS_PER_MS)
        monkeypatch.setattr("pm_nautilus.capture_feed.time_ns", clock.timestamp_ns)
        runtime = create_runtime(tmp_path / "test.sqlite", clock=clock)
        recorder = CaptureRecorder(directory, min_free_bytes=0, min_free_percent=0)
        service = MarketService(runtime, capture=recorder)
        try:
            recorder.start()
            async with asyncio.timeout(2):
                while not recorder.accepting:
                    await asyncio.sleep(0.01)
            runtime.update_preferences(PREFS, capital="1")
            runtime.add_tokens(tokens())
            runtime.book("1", [(15_000, 50_000_000)], [(20_000, 50_000_000)])
            runtime.book("2", [(940_000, 50_000_000)], [(950_000, 50_000_000)])
            runtime.start()
            await recorder.seal()
            clock.set_time((START + 2_000) * NS_PER_MS)
            runtime.book("1", [(450_000, 50_000_000)], [(600_000, 50_000_000)])
            await recorder.seal()
            return runtime.cash(), runtime.business["realized"]
        finally:
            await service.close()
            await recorder.close()
            runtime.close()

    assert asyncio.run(record()) == (22_500_000, 21_500_000)
    packed = tmp_path / "stitched.ndjson.gz"
    main([str(packed), "--directory", str(directory)])
    with open_replay_input(directory) as direct, open_replay_input(packed) as exported:
        request = dict(preferences=PREFS, starting_cash="1")
        result = run_backtest(direct, **request)
        assert run_backtest(exported, **request) == result
    assert result["summary"]["endingCash"] == "22.5"
    assert result["summary"]["realizedPnl"] == "21.5"
    assert [(r["side"], r["price"]) for r in result["fills"]] == [
        ("BUY", "0.02"),
        ("SELL", "0.45"),
    ]
