"""Full ledger validation uses a separate reader without accepting stale results."""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient

from pm_nautilus.app import create_app
from pm_nautilus.store import Store
from test_runtime import setup


async def wait_for_thread(event):
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 3), 4)


def test_validation_reader_allows_loop_work_and_checks_current_business(tmp_path, monkeypatch):
    async def run():
        runtime, _, token = setup(tmp_path)
        runtime.book(token.token_id, [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        runtime.start()
        runtime.pause()
        assert runtime.validate()["ok"]

        original = Store._read_integrity
        entered, release, finished = Event(), Event(), Event()

        def gated_read(path, stop):
            try:
                # Hold a genuine WAL read snapshot while the event loop writes.
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
                    reader.execute("BEGIN")
                    assert reader.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
                    entered.set()
                    assert release.wait(5)
                    return original(path, stop)
            finally:
                finished.set()

        monkeypatch.setattr(Store, "_read_integrity", staticmethod(gated_read))
        task = asyncio.create_task(runtime.validate_async())
        try:
            await wait_for_thread(entered)
            loop_progress = asyncio.Event()
            asyncio.get_running_loop().call_soon(loop_progress.set)
            await asyncio.wait_for(loop_progress.wait(), 1)
            runtime.store.put("parallel_write_probe", {"committed": True})
            assert runtime.store.get("parallel_write_probe") == {"committed": True}

            # The native/business comparison must run after the worker returns.
            cycle = runtime.business["cycles"][token.event_id]
            cycle["spent"] = cycle["budget"] + 1
            runtime.store.put("business", runtime.business)
            release.set()
            observed = await asyncio.wait_for(task, 5)
            assert finished.is_set()
            assert observed == runtime.validate()
            assert not observed["ok"]
            assert any("预算/成本" in error for error in observed["errors"])
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            runtime.close()

    asyncio.run(run())


def test_each_validation_runs_a_fresh_full_reader_serially(tmp_path, monkeypatch):
    async def run():
        runtime, _, _ = setup(tmp_path)
        original = Store._read_integrity
        entered, release = Event(), Event()
        guard = Lock()
        calls = []
        active = 0
        peak = 0

        def gated_read(path, stop):
            nonlocal active, peak
            with guard:
                calls.append(stop)
                number = len(calls)
                active += 1
                peak = max(peak, active)
            try:
                if number == 1:
                    entered.set()
                    assert release.wait(5)
                return original(path, stop)
            finally:
                with guard:
                    active -= 1

        monkeypatch.setattr(Store, "_read_integrity", staticmethod(gated_read))
        first = asyncio.create_task(runtime.validate_async())
        second = None
        try:
            await wait_for_thread(entered)
            second = asyncio.create_task(runtime.validate_async())
            await asyncio.sleep(0)
            assert len(calls) == 1 and active == 1
            release.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), 5)
            assert results == [runtime.validate()] * 2
            assert len(calls) == 2 and calls[0] is not calls[1]
            assert peak == 1 and active == 0
            assert not runtime.store._integrity_stops
        finally:
            release.set()
            await asyncio.gather(first, *(x for x in (second,) if x), return_exceptions=True)
            runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["reset", "close", "cancel"])
def test_lifecycle_change_rejects_inflight_validation_and_finishes_reader(
    tmp_path, monkeypatch, change
):
    async def run():
        runtime, _, _ = setup(tmp_path)
        original = Store._read_integrity
        entered, release, finished = Event(), Event(), Event()

        def gated_read(path, stop):
            entered.set()
            try:
                while not release.wait(0.01):
                    if stop.is_set():
                        break
                return original(path, stop)
            finally:
                finished.set()

        monkeypatch.setattr(Store, "_read_integrity", staticmethod(gated_read))
        task = asyncio.create_task(runtime.validate_async())
        try:
            await wait_for_thread(entered)
            if change == "reset":
                previous_generation = runtime.store.generation
                runtime.store.reset()
                assert runtime.store.generation != previous_generation
                release.set()
                with pytest.raises(ValueError, match="状态已更改"):
                    await asyncio.wait_for(task, 5)
            elif change == "close":
                runtime.close()
                with pytest.raises(ValueError, match="状态已更改"):
                    await asyncio.wait_for(task, 5)
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            assert finished.is_set()
            assert not runtime.store._integrity_stops
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            if not runtime.store._closed:
                runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["integrity_failed", "reader_error"])
def test_failed_integrity_reader_fails_closed_without_writing_ledger(
    tmp_path, monkeypatch, failure
):
    async def run():
        runtime, _, _ = setup(tmp_path)
        try:
            assert runtime.validate()["ok"]
            tables = ("meta", "native_events", "tokens", "books", "intents")

            def ledger_snapshot():
                return {
                    table: [
                        tuple(row) for row in runtime.store.db.execute(f"SELECT * FROM {table}")
                    ]
                    for table in tables
                }

            before = ledger_snapshot()
            changes = runtime.store.db.total_changes

            def failed_reader(path, stop):
                if failure == "reader_error":
                    raise sqlite3.DatabaseError("read-only integrity check failed")
                return False

            monkeypatch.setattr(Store, "_read_integrity", staticmethod(failed_reader))
            result = await runtime.validate_async()
            assert result == {"ok": False, "errors": ["SQLite integrity"], "mode": "TEST"}
            assert runtime.store.db.total_changes == changes
            assert ledger_snapshot() == before
            assert not runtime.store._integrity_stops
        finally:
            runtime.close()

    asyncio.run(run())


def test_app_remains_responsive_during_full_test_validation(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("PM_UI_PASSWORD", raising=False)
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        original = Store._read_integrity
        entered, release, finished = Event(), Event(), Event()

        def gated_read(path, stop):
            entered.set()
            try:
                assert release.wait(5)
                return original(path, stop)
            finally:
                finished.set()

        monkeypatch.setattr(Store, "_read_integrity", staticmethod(gated_read))
        with ThreadPoolExecutor(max_workers=4) as pool:
            validation = pool.submit(client.get, "/api/TEST/validation")
            try:
                assert entered.wait(3)
                health = pool.submit(client.get, "/api/health")
                dashboard = pool.submit(client.get, "/api/dashboard?mode=TEST")
                locked_live = pool.submit(client.get, "/api/LIVE/validation")
                assert health.result(timeout=3).status_code == 200
                assert dashboard.result(timeout=3).status_code == 200
                live = locked_live.result(timeout=3)
                assert live.status_code == 200
                assert live.json() == {"ok": False, "errors": ["LIVE未连接"]}
                assert not validation.done()
            finally:
                release.set()
            checked = validation.result(timeout=5)
            assert finished.is_set()
            assert checked.status_code == 200
            assert checked.json() == {"ok": True, "errors": [], "mode": "TEST"}
