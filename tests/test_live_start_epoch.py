"""An accepted LIVE START cannot outlive a later explicit PAUSE."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from test_execution_modes import enable, wallet_app
from test_live_wallet_api import _csrf
from test_wallet_cancellation import endpoint


def test_live_pause_invalidates_start_during_readiness(tmp_path, monkeypatch):
    with wallet_app(tmp_path, monkeypatch) as (app, client, state):
        headers = _csrf(client)
        assert enable(client, headers).status_code == 200
        owner = state["live"][0]
        entered, release = Event(), Event()

        def readiness():
            entered.set()
            assert release.wait(5)

        state["readiness"] = readiness
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post, "/api/LIVE/start", headers=headers)
            try:
                assert entered.wait(5)
                paused = client.post("/api/LIVE/pause", headers=headers)
                assert paused.status_code == 200
                assert client.portal.call(lambda: owner.status) == "PAUSED"
            finally:
                release.set()
            rejected = pending.result(timeout=5)
        assert rejected.status_code == 400
        assert "状态已更改" in rejected.json()["error"]
        assert client.portal.call(lambda: owner.status) == "PAUSED"
        assert owner.client._submit_order.await_count == 0
        state["readiness"] = None
        assert client.post("/api/LIVE/start", headers=headers).status_code == 200
        assert client.portal.call(lambda: owner.status) == "RUNNING"
        assert client.post("/api/LIVE/pause", headers=headers).status_code == 200


def test_live_pause_invalidates_start_queued_behind_wallet_check(tmp_path, monkeypatch):
    with wallet_app(tmp_path, monkeypatch) as (app, client, state):
        headers = _csrf(client)
        assert enable(client, headers).status_code == 200
        owner = state["live"][0]
        entered, release = Event(), Event()

        def readiness():
            entered.set()
            assert release.wait(5)

        state["readiness"] = readiness

        async def queued_start():
            pending = asyncio.create_task(endpoint(app, "/api/{mode}/start")("LIVE"))
            try:
                await asyncio.sleep(0)
                assert not pending.done()
                owner.pause()
                release.set()
                error = None
                try:
                    await pending
                except ValueError as exc:
                    error = str(exc)
                return error, owner.status, bool(owner.store.intents())
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)
                owner.pause()

        with ThreadPoolExecutor(max_workers=1) as pool:
            checking = pool.submit(client.post, "/api/live/wallet/check", headers=headers, json={})
            try:
                assert entered.wait(5)
                error, status, has_intents = client.portal.call(queued_start)
            finally:
                release.set()
            assert checking.result(timeout=5).status_code == 200
        assert error is not None and "状态已更改" in error
        assert status == "PAUSED" and not has_intents
        assert owner.client._submit_order.await_count == 0


@pytest.mark.parametrize("phase", ["storage", "validation"])
def test_live_runtime_pause_invalidates_every_start_preparation_await(tmp_path, monkeypatch, phase):
    with wallet_app(tmp_path, monkeypatch) as (_app, client, state):
        assert enable(client, _csrf(client)).status_code == 200
        owner = state["live"][0]

        async def run():
            entered, release = asyncio.Event(), asyncio.Event()
            original_guard = owner.storage_guard
            original_validation = owner.validate_async

            class StorageGate:
                can_open = True

                async def refresh(self):
                    entered.set()
                    await release.wait()

                def require_safe(self):
                    pass

            async def validation():
                entered.set()
                await release.wait()
                return await original_validation()

            if phase == "storage":
                owner.storage_guard = StorageGate()
            else:
                monkeypatch.setattr(owner, "validate_async", validation)
            pending = asyncio.create_task(owner.start_async())
            try:
                await asyncio.wait_for(entered.wait(), 5)
                owner.pause()
                release.set()
                error = None
                try:
                    await asyncio.wait_for(pending, 5)
                except ValueError as exc:
                    error = str(exc)
                status, has_intents = owner.status, bool(owner.store.intents())
                await owner.start_async()
                return error, status, has_intents, owner.status
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)
                owner.storage_guard = original_guard
                owner.pause()

        error, status, has_intents, fresh_status = client.portal.call(run)
        assert error is not None and "状态已更改" in error
        assert status == "PAUSED" and not has_intents
        assert fresh_status == "RUNNING"
        assert owner.client._submit_order.await_count == 0
