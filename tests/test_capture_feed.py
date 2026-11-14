import asyncio
from dataclasses import replace

import msgspec

from pm_nautilus.backtest_engine import run_backtest
from pm_nautilus.capture import CAPTURE_SCOPE, CaptureRecorder
from pm_nautilus.market import MarketService
from test_backtest_engine import END, NS_PER_MS, START, header, tokens
from test_runtime import setup


class Tape:
    epoch = 0
    accepting = True

    def __init__(self):
        self.rows = []

    def submit_many(self, rows):
        self.rows.extend(rows)
        return True


def change(token_id, side, price, qty):
    return dict(asset_id=token_id, side=side, price=price, size=qty)


def frame(*changes, at):
    return {
        "event_type": "price_change",
        "market": "condition",
        "timestamp": str(at),
        "price_changes": list(changes),
    }


def test_recorded_trade_can_be_replayed_and_keeps_bid_depth_after_asks_leave_cap(
    tmp_path, monkeypatch
):
    async def run():
        runtime, clock, _ = setup(tmp_path)
        runtime.add_tokens(tokens())
        runtime.update_preferences({"maxBuyPriceCents": "3"}, capital="1")
        clock.set_time(START * NS_PER_MS)
        monkeypatch.setattr("pm_nautilus.capture_feed.time_ns", clock.timestamp_ns)
        tape = Tape()
        service = MarketService(runtime, capture=tape)
        try:
            runtime.book("1", [(15_000, 50_000_000)], [(60_000, 50_000_000)])
            runtime.book("2", [(940_000, 50_000_000)], [(950_000, 50_000_000)])
            assert tape.rows == []
            runtime.start()
            opening = next(row for row in tape.rows if row["type"] == "book_batch")
            assert all(
                not row["asks"] and not row["bids"] and row["bidCovered"] is False
                for row in opening["books"]
            )
            clock.set_time((START + 1000) * NS_PER_MS)
            service.message(
                frame(
                    change("1", "SELL", "0.02", "50"),
                    change("1", "BUY", "0.016", "10"),
                    at=START + 1000,
                ),
                {"1", "2"},
            )
            promotion = next(row for row in tape.rows if row["type"] == "delta_batch")
            assert promotion["changes"] == []
            promoted = promotion["books"][0]
            assert promoted["bids"] == [(15_000, 50_000_000), (16_000, 10_000_000)]
            assert promoted["asks"] == [(20_000, 50_000_000)]
            clock.set_time((START + 2000) * NS_PER_MS)
            service.message(
                frame(
                    change("1", "SELL", "0.02", "0"),
                    change("1", "BUY", "0.45", "50"),
                    at=START + 2000,
                ),
                {"1", "2"},
            )
            clock.set_time((START + 3000) * NS_PER_MS)
            runtime.pause()
            assert [row["type"] for row in tape.rows[-2:]] == ["control", "gap"]
            result = run_backtest(
                map(
                    msgspec.json.encode,
                    [header() | {"captureScope": CAPTURE_SCOPE}, *tape.rows],
                ),
                starting_cash="1",
                preferences={"maxBuyPriceCents": "3"},
            )
            assert [(row["side"], row["price"]) for row in result["fills"]] == [
                ("BUY", "0.02"),
                ("SELL", "0.45"),
            ]
            assert result["summary"]["realizedPnl"] == "21.5"
        finally:
            await service.close()
            runtime.close()

    asyncio.run(run())


def test_writer_epoch_requires_new_baseline_and_removals_emit_gap(tmp_path, monkeypatch):
    async def run():
        runtime, clock, _ = setup(tmp_path)
        runtime.add_tokens(tokens())
        runtime.update_preferences({"maxBuyPriceCents": "3"})
        clock.set_time(START * NS_PER_MS)
        monkeypatch.setattr("pm_nautilus.capture_feed.time_ns", clock.timestamp_ns)
        tape = Tape()
        service = MarketService(runtime, capture=tape)
        try:
            runtime.start()
            runtime.book("1", [], [(50_000, 10_000_000)])
            tape.epoch += 1
            length = len(tape.rows)
            runtime.book("1", [], [(40_000, 10_000_000)])
            assert len(tape.rows) == length
            service.capture_control()
            assert [row["type"] for row in tape.rows[length : length + 2]] == ["gap", "scope"]
            assert [row["asks"] for row in tape.rows[-3]["books"]] == [[(40_000, 10_000_000)]]
            runtime.monitored.remove("1")
            service.capture_control()
            assert tape.rows[-2]["tokenIds"] == ["1"]
            assert tape.rows[-2]["detached"] is True
        finally:
            await service.close()
            runtime.close()

    asyncio.run(run())


def test_market_capture_baseline_fits_queue_at_current_subscription_scale(tmp_path, monkeypatch):
    async def run():
        runtime, clock, token = setup(tmp_path)
        clock.set_time(START * NS_PER_MS)
        monkeypatch.setattr("pm_nautilus.capture_feed.time_ns", clock.timestamp_ns)
        # 12,303 is the observed production subscription size. Including
        # 200 dense books represents many concurrent hypothetical holdings.
        token_ids = [str(i) for i in range(1, 12_304)]
        template = replace(token, opened_ns=START * NS_PER_MS - 1, ends_ns=END * NS_PER_MS)
        runtime.tokens.update(
            (
                tid,
                replace(template, token_id=tid, event_id=tid, condition_id=tid, market_id=tid),
            )
            for tid in token_ids
        )
        runtime.monitored = set(token_ids)
        prices = [(i * 1000, 10_000_000) for i in range(1, 1000)]
        for tid in token_ids[:200]:
            runtime.books[tid].snapshot(prices, [(50_000, 10_000_000)], START * NS_PER_MS)
        for tid in token_ids[200:]:
            runtime.books[tid].snapshot([], [(60_000, 10_000_000)], START * NS_PER_MS)
        recorder = CaptureRecorder(tmp_path / "capture", min_free_bytes=0, min_free_percent=0)
        service = MarketService(runtime, capture=recorder)
        try:
            recorder.start()
            for _ in range(100):
                if recorder.accepting:
                    break
                await asyncio.sleep(0.01)
            runtime.store.put("status", "RUNNING")
            service.capture_control()
            status = recorder.status()
            assert not service.capture.needs_baseline
            assert 0 < status["queueBytes"] < recorder.max_queue_bytes
            await recorder.seal()
            assert len(recorder.archives()) == 1
        finally:
            await service.close()
            await recorder.close()
            runtime.close()

    asyncio.run(run())
