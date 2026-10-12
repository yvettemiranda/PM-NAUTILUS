"""Cold read projections yield without dropping basis, gaps or lifecycle gates."""

import asyncio
import json

import pytest

import pm_nautilus.views as views
from pm_nautilus.performance import PerformanceSampler
from test_runtime import setup


def fill_history(runtime, count):
    runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    runtime.start()
    runtime.pause()
    payload = json.loads(
        runtime.store.db.execute(
            "SELECT payload FROM native_events WHERE kind='OrderFilled' LIMIT 1"
        ).fetchone()[0]
    )
    intent = next(iter(runtime.store.intents().values()))
    with runtime.store.transaction():
        runtime.store.db.execute("DELETE FROM native_events")
        runtime.store.db.execute("DELETE FROM intents")
        for number in range(count):
            oid = f"HISTORY-{number}"
            append_fill(runtime, payload, oid, oid)
            runtime.store.save_intent(oid, intent)
    return payload, intent


def append_fill(runtime, source, oid, label, quantity=50, amount=1_000_000, side="BUY"):
    payload = json.loads(json.dumps(source))
    payload.update(
        client_order_id=oid, trade_id=label, order_side=side, last_qty=f"{quantity}.000000"
    )
    payload["info"].update(pm_amount=amount, pm_gross=quantity * 1_000_000)
    runtime.store.db.execute(
        "INSERT INTO native_events(event_id,kind,payload) VALUES(?,?,?)",
        (label, "OrderFilled", json.dumps(payload).encode()),
    )


async def with_ticker(operation):
    ticks = 0
    done = False

    async def ticker():
        nonlocal ticks
        while not done:
            ticks += 1
            await asyncio.sleep(0)

    tick_task = asyncio.create_task(ticker())
    try:
        result = await operation
    finally:
        done = True
        await tick_task
    return result, ticks


def test_async_record_rebuild_yields_and_preserves_eviction_late_fill_basis(tmp_path, monkeypatch):
    async def run():
        runtime, _, _ = setup(tmp_path)
        monkeypatch.setattr(views, "RECORD_CACHE_ORDERS", 2)
        monkeypatch.setattr(views, "PROJECTION_BATCH_ROWS", 8)
        source, intent = fill_history(runtime, 200)
        expected = views.records(runtime, 2)
        runtime._record_projection = None
        first, ticks = await with_ticker(views.records_async(runtime, 2))
        assert ticks >= 10 and first == expected
        # The retired first order stays retired but its late BUY changes basis.
        append_fill(runtime, source, "HISTORY-0", "late-buy", 10, 200_000)
        assert await views.records_async(runtime, 2) == expected
        runtime.store.save_intent("final-sell", intent | {"side": "SELL", "kind": "TARGET"})
        append_fill(runtime, source, "final-sell", "final-sell", 10010, 200_300_000, "SELL")
        final = await views.records_async(runtime, 2)
        assert final["totalCount"] == 201
        assert final["records"][0]["type"] == "CLOSE"
        assert final["records"][0]["realizedPnl"] == "0.1"
        assert runtime._record_projection.qty == runtime._record_projection.basis == {}
        assert views.records(runtime, 2) == final
        runtime.close()

    asyncio.run(run())


def test_async_record_head_snapshot_and_concurrent_readers(tmp_path, monkeypatch):
    async def run():
        runtime, _, _ = setup(tmp_path)
        monkeypatch.setattr(views, "PROJECTION_BATCH_ROWS", 2)
        source, _ = fill_history(runtime, 20)
        expected = views.records(runtime, 20)
        runtime._record_projection = None
        apply = views._RecordProjection.apply_payload
        scheduled = False

        def observed(self, owner, seq, payload):
            nonlocal scheduled
            apply(self, owner, seq, payload)
            if not scheduled:
                scheduled = True
                asyncio.get_running_loop().call_soon(
                    append_fill, runtime, source, "HISTORY-0", "concurrent-late-buy", 10, 200_000
                )

        with monkeypatch.context() as patch:
            patch.setattr(views._RecordProjection, "apply_payload", observed)
            first, second = await asyncio.gather(
                views.records_async(runtime, 20), views.records_async(runtime, 20)
            )
        assert first == expected
        assert second["totalCount"] == 20
        assert second["records"][-1]["quantity"] == "60"
        assert second["records"][-1]["amount"] == "1.2"
        assert views.records(runtime, 20) == second
        runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["cancel", "reset", "close"])
def test_async_record_lifecycle_change_discards_partial_projection(tmp_path, monkeypatch, change):
    async def run():
        runtime, _, _ = setup(tmp_path)
        source, _ = fill_history(runtime, 20)
        del source
        monkeypatch.setattr(views, "PROJECTION_BATCH_ROWS", 2)
        apply = views._RecordProjection.apply_payload
        changed = False

        def observed(self, owner, seq, payload):
            nonlocal changed
            apply(self, owner, seq, payload)
            if not changed:
                changed = True
                action = (
                    asyncio.current_task().cancel
                    if change == "cancel"
                    else runtime.store.reset
                    if change == "reset"
                    else runtime.store.close
                )
                asyncio.get_running_loop().call_soon(action)

        with monkeypatch.context() as patch:
            patch.setattr(views._RecordProjection, "apply_payload", observed)
            with pytest.raises(asyncio.CancelledError if change == "cancel" else ValueError):
                await views.records_async(runtime, 20)
        assert runtime._record_projection is None
        assert not runtime._record_projection_lock.locked()
        if change == "cancel":
            assert (await views.records_async(runtime, 20))["totalCount"] == 20
        elif change == "reset":
            assert await views.records_async(runtime, 20) == {"records": [], "totalCount": 0}
        if runtime.store._closed:
            runtime.native.stop()
        else:
            runtime.close()

    asyncio.run(run())


def equity_history(runtime, count):
    sampler = PerformanceSampler(runtime)
    with runtime.store.transaction():
        runtime.store.db.executemany(
            "INSERT INTO equity_samples(generation,session,at_ms,pnl,total) VALUES(?,?,?,?,?)",
            (
                (
                    runtime.store.generation,
                    "first" if number < count // 2 else "second",
                    number * 60_000,
                    None if number % 117 == 0 else number,
                    100_000_000,
                )
                for number in range(count)
            ),
        )
    return sampler


def test_async_curve_rebuild_yields_and_preserves_complete_gap_history(tmp_path):
    async def run():
        runtime, _, _ = setup(tmp_path)
        first = equity_history(runtime, 3000)
        expected = first.view()
        sampler = PerformanceSampler(runtime)
        observed, ticks = await with_ticker(sampler.view_async())
        assert ticks >= 5 and observed == expected
        runtime.store.db.execute(
            "INSERT INTO equity_samples(generation,session,at_ms,pnl,total) VALUES(?,?,?,?,?)",
            (runtime.store.generation, "third", 4000 * 60_000, 3_000_000, 103_000_000),
        )
        first, second = await asyncio.gather(sampler.view_async(), sampler.view_async())
        assert first == second == sampler.view()
        assert first["points"][-1]["breakBefore"] is True
        runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["cancel", "reset", "close"])
def test_async_curve_lifecycle_change_discards_partial_curve(tmp_path, monkeypatch, change):
    async def run():
        runtime, _, _ = setup(tmp_path)
        sampler = equity_history(runtime, 1000)
        apply = sampler._apply_sample
        changed = False

        def observed(row):
            nonlocal changed
            apply(row)
            if not changed:
                changed = True
                action = (
                    asyncio.current_task().cancel
                    if change == "cancel"
                    else runtime.store.reset
                    if change == "reset"
                    else runtime.store.close
                )
                asyncio.get_running_loop().call_soon(action)

        with monkeypatch.context() as patch:
            patch.setattr(sampler, "_apply_sample", observed)
            with pytest.raises(asyncio.CancelledError if change == "cancel" else ValueError):
                await sampler.view_async()
        assert sampler._curve_series == {"H": [], "D": []}
        assert not sampler._curve_lock.locked()
        if change == "cancel":
            assert len((await sampler.view_async())["points"]) > 0
        elif change == "reset":
            assert (await sampler.view_async())["points"] == []
        if runtime.store._closed:
            runtime.native.stop()
        else:
            runtime.close()

    asyncio.run(run())
