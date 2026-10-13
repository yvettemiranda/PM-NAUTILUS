"""Metadata index, SQL identity retention and fresh asynchronous start checks."""

import asyncio
from dataclasses import replace
from threading import Event

import pytest
from nautilus_trader.model.events import OrderFilled

from pm_nautilus.market import MarketService
from pm_nautilus.store import Store
from pm_nautilus.strategy import Runtime, TokenRegistry
from test_async_validation import wait_for_thread
from test_runtime import setup


def test_condition_index_tracks_mapping_mutations_and_recovery(tmp_path):
    runtime, clock, t = setup(tmp_path)
    other = replace(t, token_id="2")
    runtime.add_tokens([other])
    assert runtime.condition_token_ids(t.condition_id) == ("1", "2")
    runtime.tokens["2"] = replace(other, condition_id="new-condition")
    assert runtime.condition_token_ids(t.condition_id) == ("1",)
    assert runtime.condition_token_ids("new-condition") == ("2",)
    runtime.tokens.update({"3": replace(t, token_id="3")})
    runtime.tokens.setdefault("4", replace(t, token_id="4"))
    assert runtime.condition_token_ids(t.condition_id) == ("1", "3", "4")
    runtime.tokens.pop("3")
    del runtime.tokens["4"]
    assert runtime.condition_token_ids(t.condition_id) == ("1",)
    runtime.close()
    runtime = Runtime(tmp_path / "test.sqlite", clock=clock)
    try:
        assert runtime.condition_token_ids(t.condition_id) == ("1", "2")
        index = TokenRegistry({"1": t})
        index |= {"2": other}
        assert index.popitem() == ("2", other)
        assert index.condition_token_ids(t.condition_id) == ("1",)
        index.clear()
        assert not index.condition_token_ids(t.condition_id)
    finally:
        runtime.close()


def test_release_unmonitored_retains_execution_identity_without_decoding_history(
    tmp_path, monkeypatch
):
    runtime, _, t = setup(tmp_path)
    # Closed historical tokens remain necessary for native replay and settlement.
    runtime.store.save_intent(
        "historic",
        {"token_id": t.token_id, "event_id": t.event_id, "side": "BUY", "terminal": True},
    )
    runtime.add_tokens([replace(t, open=False), replace(t, token_id="2", open=False)])
    original = runtime.store.intents

    def no_full_history(*args, **kwargs):
        if not kwargs.get("active_only"):
            raise AssertionError("historical intent payloads were decoded")
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.store, "intents", no_full_history)
    try:
        runtime.monitored.clear()
        runtime.release_unmonitored()
        assert t.token_id in runtime.tokens and "2" not in runtime.tokens
        assert runtime.condition_token_ids(t.condition_id) == ("1",)
    finally:
        runtime.close()


def test_native_delivery_projects_durable_prefix_without_future_facts(tmp_path, monkeypatch):
    runtime, _, t = setup(tmp_path)
    try:
        runtime.book(t.token_id, [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        runtime.start()
        journal = runtime.store.events()
        fill_seq, fill = next(
            (seq, event) for seq, event in journal if isinstance(event, OrderFilled)
        )
        frozen = dict(fill.info["pm_rules"])
        # Simulate a later durable fact awaiting its own native delivery. Only
        # the prefix through this Fill is eligible for the current callback.
        runtime.store.db.execute(
            "INSERT INTO native_events(event_id,kind,payload) "
            "SELECT 'controlled-future',kind,payload FROM native_events WHERE seq=?",
            (fill_seq,),
        )
        runtime.business["cursor"] = 0
        projected = []

        def capture(seq, event):
            projected.append((seq, event))
            runtime.business["cursor"] = seq

        monkeypatch.setattr(runtime, "project", capture)
        fill.info["pm_rules"]["budget"] = frozen["budget"] + 1
        runtime.on_native(fill)
        assert [seq for seq, _ in projected] == [seq for seq, _ in journal if seq <= fill_seq]
        assert runtime.business["cursor"] == fill_seq
        assert projected[-1][1].info["pm_rules"] == frozen
        runtime.faulted = "controlled projection fault"
        runtime.business["cursor"] = 0
        runtime.on_native(fill)
        assert runtime.business["cursor"] == 0
    finally:
        runtime.close()


@pytest.mark.parametrize("change", ["none", "pause", "invalid_business"])
def test_async_start_offloads_integrity_and_checks_current_owner_state(
    tmp_path, monkeypatch, change
):
    async def run():
        runtime, _, t = setup(tmp_path)
        runtime.book(t.token_id, [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        service = MarketService(runtime)
        entered, release, finished = Event(), Event(), Event()
        original = Store._read_integrity

        def pending_integrity(path, stop):
            try:
                entered.set()
                assert release.wait(5)
                return original(path, stop)
            finally:
                finished.set()

        monkeypatch.setattr(Store, "_read_integrity", staticmethod(pending_integrity))
        monkeypatch.setattr(
            runtime,
            "validate",
            lambda: (_ for _ in ()).throw(AssertionError("synchronous integrity check")),
        )
        service.remember_error("serviceError", RuntimeError("old fault"))
        task = asyncio.create_task(service.start_async())
        try:
            await wait_for_thread(entered)
            progressed = asyncio.Event()
            asyncio.get_running_loop().call_soon(progressed.set)
            await asyncio.wait_for(progressed.wait(), 1)
            assert runtime.status == "PAUSED" and not runtime.store.intents()
            if change == "pause":
                runtime.pause()
            elif change == "invalid_business":
                runtime.business["recovery_error"] = "changed while checking"
            release.set()
            if change == "none":
                await asyncio.wait_for(task, 5)
                assert runtime.status == "RUNNING" and len(runtime.store.intents()) == 1
                assert "serviceError" not in service.scan_status
            else:
                with pytest.raises(ValueError):
                    await asyncio.wait_for(task, 5)
                assert runtime.status == "PAUSED" and not runtime.store.intents()
                assert service.scan_status["serviceError"] == "RuntimeError"
            assert finished.is_set()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await service.close()
            runtime.close()

    asyncio.run(run())
