"""LIVE cleanup keeps owning adapters and native queues after control failures."""

import asyncio
import fcntl
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import pm_nautilus.app as app_module
from pm_nautilus.strategy import Runtime
from test_lifecycle import controlled_factory


async def force_close(runtime):
    """Release the deliberately broken fixture even when a regression fails."""
    pending = [task for task in runtime.client._tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    engine = runtime.native.execution
    queues = [engine.get_cmd_queue_task(), engine.get_evt_queue_task()]
    if any(task is not None and not task.done() for task in queues):
        runtime.native.stop()
        await asyncio.gather(*(task for task in queues if task), return_exceptions=True)
    if not runtime.store._closed:
        runtime.store.close()


async def pending_response(runtime):
    entered = asyncio.Event()

    async def response():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            runtime.store.put("shutdown_response_finalized", True)

    task = runtime.client.create_task(response())
    await entered.wait()
    return task


def no_grace(monkeypatch, failure=None):
    original = asyncio.wait
    raised = False

    async def wait(tasks, timeout):
        nonlocal raised
        if failure is not None and not raised:
            raised = True
            raise failure
        return await original(tasks, timeout=0)

    monkeypatch.setattr("pm_nautilus.live.asyncio.wait", wait)


@pytest.mark.parametrize("failure_point", ["pause", "drain"])
def test_shutdown_control_error_still_joins_every_owned_task(tmp_path, monkeypatch, failure_point):
    async def run():
        runtime = Runtime(tmp_path / "LIVE.sqlite", mode="LIVE", live_factory=controlled_factory)
        task = await pending_response(runtime)
        failure = sqlite3.OperationalError("controlled shutdown control failure")
        if failure_point == "pause":
            monkeypatch.setattr(runtime, "pause", lambda: (_ for _ in ()).throw(failure))
            no_grace(monkeypatch)
        else:
            no_grace(monkeypatch, failure)
        try:
            with pytest.raises(sqlite3.OperationalError) as observed:
                await runtime.client.shutdown()
            assert observed.value is failure
            runtime.client._disconnect.assert_awaited_once()
            assert task.cancelled()
            assert runtime.store.get("shutdown_response_finalized") is True
            assert not runtime.store._closed
            assert runtime.native.execution.get_cmd_queue_task().done()
            assert runtime.native.execution.get_evt_queue_task().done()
            assert all(t.done() for t in runtime.client._tasks)
        finally:
            await force_close(runtime)

    asyncio.run(run())


def test_shutdown_keeps_primary_error_when_disconnect_also_fails(tmp_path, monkeypatch):
    async def run():
        runtime = Runtime(tmp_path / "LIVE.sqlite", mode="LIVE", live_factory=controlled_factory)
        task = await pending_response(runtime)
        failure = sqlite3.OperationalError("controlled pause failure")
        monkeypatch.setattr(runtime, "pause", lambda: (_ for _ in ()).throw(failure))
        runtime.client._disconnect.side_effect = ValueError("controlled disconnect failure")
        no_grace(monkeypatch)
        try:
            with pytest.raises(sqlite3.OperationalError) as observed:
                await runtime.client.shutdown()
            assert observed.value is failure
            runtime.client._disconnect.assert_awaited_once()
            assert task.cancelled()
            assert runtime.store.get("shutdown_response_finalized") is True
            assert runtime.native.execution.get_cmd_queue_task().done()
            assert runtime.native.execution.get_evt_queue_task().done()
        finally:
            await force_close(runtime)

    asyncio.run(run())


def test_repeated_shutdown_cancellation_waits_for_actual_cleanup(tmp_path, monkeypatch):
    async def run():
        runtime = Runtime(tmp_path / "LIVE.sqlite", mode="LIVE", live_factory=controlled_factory)
        response = await pending_response(runtime)
        entered, release = asyncio.Event(), asyncio.Event()

        async def disconnect():
            entered.set()
            await release.wait()
            runtime.store.put("disconnect_completed", True)

        runtime.client._disconnect.side_effect = disconnect
        no_grace(monkeypatch)
        stopping = asyncio.create_task(runtime.client.shutdown())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            stopping.cancel()
            await asyncio.sleep(0)
            stopping.cancel()
            await asyncio.sleep(0)
            assert not stopping.done()
            assert not runtime.store._closed
            assert not runtime.native.execution.get_cmd_queue_task().done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await stopping
            assert runtime.store.get("disconnect_completed") is True
            assert runtime.store.get("shutdown_response_finalized") is True
            assert response.cancelled()
            assert runtime.native.execution.get_cmd_queue_task().done()
            assert runtime.native.execution.get_evt_queue_task().done()
        finally:
            release.set()
            await asyncio.gather(stopping, return_exceptions=True)
            await force_close(runtime)

    asyncio.run(run())


def test_lifespan_retains_ledger_and_process_lock_until_fault_cleanup_finishes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PM_LIVE_ENABLED", "true")
    monkeypatch.setenv("PM_UI_PASSWORD", "controlled-local-password")
    monkeypatch.setattr("pm_nautilus.live.load_settings", lambda: {})
    monkeypatch.setattr("pm_nautilus.live.live_factory", lambda *_: controlled_factory)
    monkeypatch.setattr(
        "pm_nautilus.redemption.PolygonWallet",
        lambda _: SimpleNamespace(preflight=lambda: None),
    )
    owners = []
    factory = app_module.Runtime

    def runtime(*args, **kwargs):
        owner = factory(*args, **kwargs)
        if owner.mode == "LIVE":
            owner.client.activate = AsyncMock()
            owners.append(owner)
        return owner

    monkeypatch.setattr(app_module, "Runtime", runtime)

    async def run():
        app = app_module.create_app(tmp_path, public_data=False)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        owner = owners[0]
        response = await pending_response(owner)
        entered, release = asyncio.Event(), asyncio.Event()

        def failed_pause():
            raise sqlite3.OperationalError("controlled shutdown pause failure")

        async def disconnect():
            entered.set()
            await release.wait()
            owner.store.put("disconnect_completed", True)

        monkeypatch.setattr(owner, "pause", failed_pause)
        owner.client._disconnect.side_effect = disconnect
        no_grace(monkeypatch)
        stopping = asyncio.create_task(lifespan.__aexit__(None, None, None))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert not stopping.done()
            assert not owner.store._closed
            with (tmp_path / "process.lock").open("a") as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release.set()
            with pytest.raises(RuntimeError, match="组件关闭失败"):
                await stopping
            assert owner.store._closed
            assert response.cancelled()
            assert owner.native.execution.get_cmd_queue_task().done()
            assert owner.native.execution.get_evt_queue_task().done()
            with (tmp_path / "process.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            release.set()
            await asyncio.gather(stopping, return_exceptions=True)
            await force_close(owner)

    asyncio.run(run())
