"""Explicit LIVE enable stops TEST background work before any private activation."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx
from fastapi.testclient import TestClient

import pm_nautilus.app as app_module
from pm_nautilus.market import MarketService
from pm_nautilus.execution_mode import ExecutionModeStore
from test_lifecycle import controlled_factory
from test_live_wallet_api import _csrf, _payload, _settings


RESET = {"confirmation": "RESET TEST", "finalConfirmation": "RESET TEST AGAIN"}


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("PM_UI_PASSWORD", raising=False)


async def idle(*_args):
    await asyncio.Future()


@contextmanager
def wallet_app(path, monkeypatch, *, legacy=False):
    state = {"live": [], "activate": None, "shutdown": None, "readiness": None}
    original = app_module.Runtime

    def runtime(*args, **kwargs):
        owner = original(*args, **kwargs)
        if owner.mode == "LIVE":
            state["live"].append(owner)
            shutdown = owner.client.shutdown

            async def activate():
                if state["activate"] is not None:
                    await state["activate"](owner)
                owner.client.ready = owner.client.open_orders_clear = True
                owner.client.reconciliation_task = asyncio.create_task(idle())

            async def stop():
                state["shutdownStarted"] = True
                await shutdown()
                state["shutdownFinished"] = True
                if state["shutdown"] is not None:
                    await state["shutdown"](owner)

            owner.client.activate = AsyncMock(side_effect=activate)
            owner.client.shutdown = stop
        return owner

    def readiness(*args, **kwargs):
        if state["readiness"] is not None:
            state["readiness"]()
        return {"ready": True, "checks": []}

    monkeypatch.setattr(app_module, "Runtime", runtime)
    monkeypatch.setattr(app_module, "run_readiness", readiness)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    monkeypatch.setattr("pm_nautilus.live.live_factory", lambda *_: controlled_factory)
    monkeypatch.setattr("pm_nautilus.live.load_settings", lambda: {})
    monkeypatch.setattr(
        "pm_nautilus.redemption.PolygonWallet",
        lambda _: SimpleNamespace(preflight=lambda: None),
    )
    # Real service ownership/tasks, but no public HTTP or websocket activity.
    monkeypatch.setattr(MarketService, "run", idle)
    if legacy:
        monkeypatch.setenv("PM_LIVE_ENABLED", "true")
        monkeypatch.setenv("PM_UI_PASSWORD", "controlled-local-password")
    app = app_module.create_app(path, public_data=True)
    state["app"] = app
    with TestClient(app) as client:
        if legacy:
            client.auth = ("pm", "controlled-local-password")
        else:
            imported = client.post(
                "/api/live/wallet/import", json=_payload(), headers=_csrf(client)
            )
            assert imported.status_code == 200, imported.text
        yield app, client, state


def enable(client, headers):
    return client.post(
        "/api/live/wallet/enable", json={"confirmation": "ENABLE LIVE"}, headers=headers
    )


def wait_transition(app):
    deadline = time.monotonic() + 5
    while not app.state.execution_state["transitioning"] and time.monotonic() < deadline:
        time.sleep(0.005)
    assert app.state.execution_state["transitioning"]


def test_running_test_account_prevents_live_enable_without_stopping_any_owner(
    tmp_path, monkeypatch
):
    with wallet_app(tmp_path, monkeypatch) as (app, client, state):
        headers = _csrf(client)
        started = client.post("/api/TEST/start?account_id=7", headers=headers)
        assert started.status_code == 200
        services = dict(app.state.test_services)
        result = enable(client, headers)
        assert result.status_code == 400 and "7" in result.json()["error"]
        assert state["live"] == []
        assert app.state.execution_state["testSuspended"] is False
        assert app.state.execution_state["transitioning"] is False
        assert not app.state.execution_state["cleanupUnconfirmed"]
        assert not app.state.execution_state["persistenceUnconfirmed"]
        assert client.portal.call(lambda: app.state.test_runtimes[7].status) == "RUNNING"
        assert all(
            not service.closed and not service.loop_task.done() for service in services.values()
        )


def test_enable_stops_every_test_job_before_activate_and_test_becomes_read_only(
    tmp_path, monkeypatch
):
    with wallet_app(tmp_path, monkeypatch) as (app, client, state):
        headers = _csrf(client)
        owners = dict(app.state.test_runtimes)
        services = dict(app.state.test_services)
        samplers = dict(app.state.test_samplers)

        async def add_pending_jobs():
            for service in services.values():
                service.scan_task = asyncio.create_task(idle())
                service.resolution_task = asyncio.create_task(idle())

        client.portal.call(add_pending_jobs)

        async def inspect(_owner):
            assert app.state.execution_state["testSuspended"]
            assert not app.state.market_hub.services
            for number, service in services.items():
                assert service.closed
                assert service.loop_task.done() and service.scan_task.done()
                assert service.resolution_task.done() and samplers[number].task.done()
                assert not owners[number].store._closed

        state["activate"] = inspect
        result = enable(client, headers)
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "PAUSED"
        assert app.state.test_runtimes == owners
        assert {service.runtime.mode for service in app.state.market_hub.services} == {"LIVE"}
        for number in (1, 10):
            data = client.get(f"/api/dashboard?account_id={number}").json()
            assert data["testExecutionSuspended"] is True
            assert data["activeExecutionMode"] == "LIVE" and data["accountId"] == number
            assert data["strategy"]["status"] == "PAUSED"
            assert client.get(f"/api/TEST/trade-records?account_id={number}").status_code == 200
            assert client.get(f"/api/TEST/performance?account_id={number}").status_code == 200
            for method, path, payload in (
                (client.post, "start", None),
                (client.put, "preferences", {"orderAmount": "2"}),
                (client.put, "preferences", {"candidateSortDirection": "DESC"}),
                (client.post, "reset", RESET),
            ):
                denied = method(
                    f"/api/TEST/{path}?account_id={number}", json=payload, headers=headers
                )
                assert denied.status_code == 400 and "TEST后台已停止" in denied.json()["error"]
        paused = client.post("/api/LIVE/pause", headers=headers)
        assert paused.status_code == 200
        assert app.state.execution_state["testSuspended"] is True
        assert all(service.closed for service in services.values())
        assert client.post("/api/TEST/start?account_id=2", headers=headers).status_code == 400
        assert state["live"][0].client._submit_order.await_count == 0


def test_start_during_enable_readiness_is_rejected_before_private_activation(tmp_path, monkeypatch):
    with wallet_app(tmp_path, monkeypatch) as (app, client, state):
        headers = _csrf(client)
        entered, release = threading.Event(), threading.Event()

        def readiness():
            entered.set()
            assert release.wait(5)

        state["readiness"] = readiness
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(enable, client, headers)
            try:
                assert entered.wait(5)
                assert app.state.execution_state["transitioning"]
                assert not app.state.execution_state["testSuspended"]
                denied = client.post("/api/TEST/start?account_id=9", headers=headers)
                assert denied.status_code == 400
                assert state["live"] == []
            finally:
                release.set()
            assert pending.result(timeout=5).status_code == 200


def test_accepted_test_start_validation_cannot_finish_after_mode_transition_begins(
    tmp_path, monkeypatch
):
    with wallet_app(tmp_path, monkeypatch) as (app, client, _state):
        headers = _csrf(client)
        owner = app.state.test_runtimes[9]
        original = owner.validate_async
        entered, release = threading.Event(), threading.Event()

        async def validation():
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)
            return await original()

        monkeypatch.setattr(owner, "validate_async", validation)
        with ThreadPoolExecutor(max_workers=2) as pool:
            starting = pool.submit(client.post, "/api/TEST/start?account_id=9", headers=headers)
            try:
                assert entered.wait(5)
                enabling = pool.submit(enable, client, headers)
                wait_transition(app)
                assert not enabling.done()
            finally:
                release.set()
            assert starting.result(timeout=5).status_code == 400
            assert enabling.result(timeout=5).status_code == 200
        assert client.portal.call(lambda: owner.status) == "PAUSED"
        assert client.portal.call(lambda: len(owner.store.intents())) == 0


def test_failed_activation_restores_paused_test_only_after_private_shutdown_finishes(
    tmp_path, monkeypatch
):
    with wallet_app(tmp_path, monkeypatch) as (app, client, state):
        headers = _csrf(client)
        old_services = dict(app.state.test_services)
        old_samplers = dict(app.state.test_samplers)
        entered, release = threading.Event(), threading.Event()

        async def fail(_owner):
            raise ValueError("controlled activation failure")

        async def shutdown(_owner):
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)

        state.update(activate=fail, shutdown=shutdown)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(enable, client, headers)
            try:
                assert entered.wait(5)
                assert app.state.execution_state["testSuspended"]
                assert all(service.closed for service in old_services.values())
                assert all(sampler.task.done() for sampler in old_samplers.values())
                assert (
                    client.post("/api/TEST/start?account_id=1", headers=headers).status_code == 400
                )
            finally:
                release.set()
            response = pending.result(timeout=5)
        assert response.status_code == 400
        assert app.state.execution_state["testSuspended"] is False
        assert app.state.execution_state["transitioning"] is False
        assert not app.state.execution_state["cleanupUnconfirmed"]
        assert not app.state.execution_state["persistenceUnconfirmed"]
        assert "LIVE" not in app.state.runtimes and state["live"][0].store._closed
        for number, service in app.state.test_services.items():
            assert service is not old_services[number] and not service.closed
            assert not service.loop_task.done()
            assert client.portal.call(lambda: service.runtime.status) == "PAUSED"
        assert client.get("/api/dashboard").json()["testExecutionSuspended"] is False


def test_unconfirmed_private_shutdown_leaves_all_test_background_stopped(tmp_path, monkeypatch):
    with wallet_app(tmp_path, monkeypatch) as (app, client, state):
        headers = _csrf(client)

        async def fail(_owner):
            raise ValueError("controlled activation failure")

        async def unconfirmed(_owner):
            # The fixture drained actual native queues first; only its success
            # acknowledgement fails, so no live worker is leaked by this test.
            raise ValueError("controlled shutdown acknowledgement failure")

        state.update(activate=fail, shutdown=unconfirmed)
        response = enable(client, headers)
        assert response.status_code == 400 and "保持停止" in response.json()["error"]
        assert app.state.execution_state["testSuspended"] is True
        assert app.state.execution_state["transitioning"] is False
        assert app.state.execution_state["cleanupUnconfirmed"] is True
        assert all(service.closed for service in app.state.test_services.values())
        assert client.post("/api/TEST/start?account_id=4", headers=headers).status_code == 400
        assert client.get("/api/dashboard").json()["testExecutionSuspended"] is True


def test_live_activation_waits_for_cancelled_test_settlement_cleanup(tmp_path, monkeypatch):
    with wallet_app(tmp_path, monkeypatch) as (app, client, state):
        headers = _csrf(client)
        cleaning, release, activated = threading.Event(), threading.Event(), threading.Event()

        async def pending_settlement():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleaning.set()
                assert await asyncio.to_thread(release.wait, 5)
                raise

        async def install():
            app.state.test_services[5].resolution_task = asyncio.create_task(pending_settlement())
            await asyncio.sleep(0)

        async def activate(_owner):
            activated.set()

        client.portal.call(install)
        state["activate"] = activate
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(enable, client, headers)
            try:
                assert cleaning.wait(5)
                assert not activated.is_set() and not pending.done()
                assert app.state.execution_state["testSuspended"]
                assert (
                    client.post("/api/TEST/start?account_id=8", headers=headers).status_code == 400
                )
            finally:
                release.set()
            assert pending.result(timeout=5).status_code == 200
        assert activated.is_set()


def test_cancelled_live_enable_drains_private_client_before_restoring_test(tmp_path, monkeypatch):
    with wallet_app(tmp_path, monkeypatch) as (app, client, state):
        headers = _csrf(client)

        async def run():
            entered = asyncio.Event()

            async def activate(_owner):
                entered.set()
                await asyncio.Future()

            state["activate"] = activate
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as requester:
                task = asyncio.create_task(
                    requester.post(
                        "/api/live/wallet/enable",
                        json={"confirmation": "ENABLE LIVE"},
                        headers=headers,
                    )
                )
                await asyncio.wait_for(entered.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                deadline = asyncio.get_running_loop().time() + 5
                while app.state.execution_state["transitioning"]:
                    assert asyncio.get_running_loop().time() < deadline
                    await asyncio.sleep(0.001)
                live = state["live"][0]
                assert app.state.execution_state["testSuspended"] is False, {
                    "state": app.state.execution_state,
                    "shutdownStarted": state.get("shutdownStarted"),
                    "shutdownFinished": state.get("shutdownFinished"),
                    "storeClosed": live.store._closed,
                    "cmdDone": live.native.execution.get_cmd_queue_task().done(),
                    "evtDone": live.native.execution.get_evt_queue_task().done(),
                }
                assert "LIVE" not in app.state.runtimes
                assert live.store._closed
                assert live.native.execution.get_cmd_queue_task().done()
                assert live.native.execution.get_evt_queue_task().done()
                assert all(
                    runtime.status == "PAUSED" for runtime in app.state.test_runtimes.values()
                )
                assert all(not service.closed for service in app.state.test_services.values())

        client.portal.call(run)


def test_legacy_live_boot_never_starts_test_background_jobs(tmp_path, monkeypatch):
    with wallet_app(tmp_path, monkeypatch, legacy=True) as (app, client, _state):
        assert app.state.execution_state["testSuspended"]
        assert all(service.loop_task is None for service in app.state.test_services.values())
        assert all(sampler.task is None for sampler in app.state.test_samplers.values())
        assert {service.runtime.mode for service in app.state.market_hub.services} == {"LIVE"}
        assert client.post("/api/TEST/start", headers=_csrf(client)).status_code == 400
        assert client.get("/api/dashboard").json()["testExecutionSuspended"] is True


def test_persisted_live_intent_reboot_keeps_both_modes_stopped_and_wallet_locked(
    tmp_path, monkeypatch
):
    # First persist an encrypted wallet through the ordinary local API fixture.
    # No LIVE runtime is enabled or started, on either side of this restart.
    with wallet_app(tmp_path, monkeypatch) as (_app, _client, state):
        pass
    ExecutionModeStore(tmp_path).save("LIVE")
    app = app_module.create_app(tmp_path, public_data=True)
    with TestClient(app) as client:
        assert "LIVE" not in app.state.runtimes and state["live"] == []
        assert all(service.loop_task is None for service in app.state.test_services.values())
        assert all(sampler.task is None for sampler in app.state.test_samplers.values())
        assert not app.state.market_hub.services
        wallet = client.get("/api/live/wallet").json()
        assert wallet["status"] == "LOCKED" and wallet["unlocked"] is False
        assert wallet["enabled"] is False
        data = client.get("/api/dashboard?account_id=10").json()
        assert data["testExecutionSuspended"] is True
        assert data["activeExecutionMode"] == "STOPPED"
        assert data["modeSwitchInProgress"] is False
        assert data["canRestoreTestBackground"] is True
        assert data["strategy"]["status"] == "PAUSED"
        assert (
            client.post("/api/TEST/start?account_id=10", headers=_csrf(client)).status_code == 400
        )
    assert ExecutionModeStore(tmp_path).read() == "LIVE"


def test_explicit_test_restore_requires_all_paused_restores_jobs_and_persists_only_test(
    tmp_path, monkeypatch
):
    ExecutionModeStore(tmp_path).save("LIVE")
    with wallet_app(tmp_path, monkeypatch) as (app, client, state):
        headers = _csrf(client)
        owners = dict(app.state.test_runtimes)
        assert all(service.loop_task is None for service in app.state.test_services.values())
        assert (
            client.post(
                "/api/TEST/restore-background?account_id=10", json={}, headers=headers
            ).status_code
            == 400
        )
        # An unexpected RUNNING state must fail closed; restoration never calls
        # START or silently changes a simulation owner's status for the caller.
        client.portal.call(owners[7].store.put, "status", "RUNNING")
        denied = client.post(
            "/api/TEST/restore-background?account_id=10",
            json={"confirmation": "RESTORE TEST"},
            headers=headers,
        )
        assert denied.status_code == 400 and "7" in denied.json()["error"]
        assert app.state.execution_state["testSuspended"] is True
        assert all(service.loop_task is None for service in app.state.test_services.values())
        assert ExecutionModeStore(tmp_path).read() == "LIVE"
        client.portal.call(owners[7].store.put, "status", "PAUSED")
        restored = client.post(
            "/api/TEST/restore-background?account_id=10",
            json={"confirmation": "RESTORE TEST"},
            headers=headers,
        )
        assert restored.status_code == 200, restored.text
        data = restored.json()
        assert data["accountId"] == 10 and data["testExecutionSuspended"] is False
        assert data["activeExecutionMode"] == "TEST" and data["strategy"]["status"] == "PAUSED"
        assert app.state.test_runtimes == owners and state["live"] == []
        assert all(
            not service.closed and not service.loop_task.done()
            for service in app.state.test_services.values()
        )
        assert all(not sampler.task.done() for sampler in app.state.test_samplers.values())
        assert client.portal.call(lambda: {owner.status for owner in owners.values()}) == {"PAUSED"}
        assert (
            client.portal.call(lambda: sum(len(owner.store.intents()) for owner in owners.values()))
            == 0
        )
        assert ExecutionModeStore(tmp_path).read() == "TEST"


def test_live_enabled_refuses_explicit_test_background_restore(tmp_path, monkeypatch):
    with wallet_app(tmp_path, monkeypatch) as (app, client, _state):
        headers = _csrf(client)
        assert enable(client, headers).status_code == 200
        denied = client.post(
            "/api/TEST/restore-background?account_id=3",
            json={"confirmation": "RESTORE TEST"},
            headers=headers,
        )
        assert denied.status_code == 400 and "LIVE" in denied.json()["error"]
        assert app.state.execution_state["testSuspended"] is True
        assert all(service.closed for service in app.state.test_services.values())
        assert {service.runtime.mode for service in app.state.market_hub.services} == {"LIVE"}
        assert ExecutionModeStore(tmp_path).read() == "LIVE"
