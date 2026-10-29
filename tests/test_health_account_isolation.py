"""A local simulation fault is observable without failing healthy account owners."""

import asyncio
import runpy
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

import pm_nautilus.app as app_module
from pm_nautilus.service_freshness import freshness_view


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("PM_UI_PASSWORD", raising=False)


def statuses(app):
    return {number: runtime.status for number, runtime in app.state.test_runtimes.items()}


def start_all(app):
    for runtime in app.state.test_runtimes.values():
        runtime.start()  # Empty local books: this creates no orders or positions.


def test_fifth_real_checkpoint_fault_stops_only_its_owner_and_health_stays_live(
    tmp_path, monkeypatch
):
    app = app_module.create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        client.portal.call(start_all, app)
        runtime = app.state.test_runtimes[5]
        monkeypatch.setattr(
            runtime.store,
            "save_book",
            Mock(side_effect=sqlite3.OperationalError("private checkpoint exception")),
        )
        with pytest.raises(sqlite3.OperationalError):
            client.portal.call(runtime.checkpoint_book, "controlled-local-token")
        before = client.portal.call(statuses, app)
        changes = client.portal.call(
            lambda: [owner.store.db.total_changes for owner in app.state.test_runtimes.values()]
        )
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "attention"
        assert data["accountErrors"] == data["backgroundErrors"] == {"TEST:5": "runtime_faulted"}
        assert data["testAccountCount"] == 10 and data["availableTestAccountCount"] == 9
        assert before[5] == "PAUSED"
        assert all(state == "RUNNING" for number, state in before.items() if number != 5)
        assert client.portal.call(statuses, app) == before
        assert (
            client.portal.call(
                lambda: [owner.store.db.total_changes for owner in app.state.test_runtimes.values()]
            )
            == changes
        )
        assert client.get("/api/dashboard?details=summary&account_id=1").status_code == 200
        assert "private" not in response.text


@pytest.mark.parametrize("diagnostic", ["market", "freshness", "supervision", "primary_status"])
def test_bad_test_diagnostic_does_not_fail_health_or_control_other_accounts(
    tmp_path, monkeypatch, diagnostic
):
    app = app_module.create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        client.portal.call(start_all, app)
        number = 1 if diagnostic == "primary_status" else 5
        service = app.state.test_services[number]
        runtime = app.state.test_runtimes[number]
        before = client.portal.call(statuses, app)
        if diagnostic == "market":
            monkeypatch.setattr(
                service, "stream_diagnostics", Mock(side_effect=ValueError("private market error"))
            )
        elif diagnostic == "freshness":
            service.resolution_status = {"private": "bad query view"}

            def broken(state):
                if state is service.resolution_status:
                    raise ValueError("private freshness error")
                return freshness_view(state)

            monkeypatch.setattr(app_module, "freshness_view", broken)
        elif diagnostic == "supervision":
            service.scan_status = None
        else:
            original = runtime.store.get

            def broken(key, *args):
                if key == "status":
                    raise sqlite3.DatabaseError("private account database error")
                return original(key, *args)

            monkeypatch.setattr(runtime.store, "get", broken)
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "attention"
        key = "TEST" if number == 1 else "TEST:5"
        assert response.json()["accountErrors"] == {key: "account_diagnostic_error"}
        assert response.json()["availableTestAccountCount"] == 9
        assert "private" not in response.text
        # Restore malformed fields before reading statuses or lifecycle cleanup.
        monkeypatch.undo()
        if diagnostic == "supervision":
            service.scan_status = {}
        assert client.portal.call(statuses, app) == before


@pytest.mark.parametrize("failure", ["runtime", "quarantined"])
def test_all_test_accounts_unavailable_are_still_fatal_without_live(tmp_path, failure):
    app = app_module.create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        for context in app.state.test_accounts.contexts.values():
            if failure == "runtime":
                context.runtime.faulted = "private runtime fault"
            else:
                context.error = "controlled initialization failure"
        response = client.get("/api/health")
        data = response.json()
        assert response.status_code == 503 and data["status"] == "degraded"
        assert len(data["accountErrors"]) == 10
        assert data["availableTestAccountCount"] == 0
        assert data["backgroundErrors"]["testAccounts"] == "no_available_test_account"
        assert "private" not in response.text


@pytest.mark.parametrize("failure", ["storage", "maintenance"])
def test_platform_fault_remains_503_with_healthy_simulation_accounts(tmp_path, failure):
    app = app_module.create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        if failure == "storage":
            app.state.storage_guard._state = "CRITICAL"
        else:
            endpoint = next(
                route.endpoint
                for route in app.routes
                if getattr(route, "path", None) == "/api/health"
            )
            cells = dict(zip(endpoint.__code__.co_freevars, endpoint.__closure__))
            task = cells["retention_task"].cell_contents

            async def stop():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            client.portal.call(stop)
        response = client.get("/api/health")
        data = response.json()
        assert response.status_code == 503 and data["status"] == "degraded"
        assert data["accountErrors"] == {}
        key = "storage" if failure == "storage" else "storageMaintenance"
        assert key in data["backgroundErrors"]


def test_healthy_live_owner_keeps_all_test_faults_local_but_live_fault_stays_fatal(tmp_path):
    app = app_module.create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        account = {"live_error": None}
        live = SimpleNamespace(
            faulted=None,
            business={"recovery_error": None},
            store=SimpleNamespace(get=lambda key: account.get(key)),
            client=SimpleNamespace(reconciliation_task=SimpleNamespace(done=lambda: False)),
        )
        service = SimpleNamespace(
            scan_status={}, loop_task=None, closed=False, stream_diagnostics=lambda: {"groups": []}
        )
        app.state.runtimes["LIVE"] = live
        app.state.services["LIVE"] = service
        try:
            for context in app.state.test_accounts.contexts.values():
                context.error = "controlled unavailable simulation"
            response = client.get("/api/health")
            assert response.status_code == 200
            assert response.json()["status"] == "attention"
            assert response.json()["availableTestAccountCount"] == 0
            account["live_error"] = "private LIVE fault"
            response = client.get("/api/health")
            assert response.status_code == 503
            assert response.json()["backgroundErrors"]["LIVE"] == "live_account_error"
            assert "private" not in response.text
        finally:
            del app.state.runtimes["LIVE"]
            del app.state.services["LIVE"]


def test_intentionally_closed_test_owners_do_not_break_active_live_health(tmp_path):
    app = app_module.create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        live = SimpleNamespace(
            faulted=None,
            business={"recovery_error": None},
            store=SimpleNamespace(get=lambda key: None),
            client=SimpleNamespace(reconciliation_task=SimpleNamespace(done=lambda: False)),
        )
        service = SimpleNamespace(
            scan_status={}, loop_task=None, closed=False, stream_diagnostics=lambda: {"groups": []}
        )
        app.state.runtimes["LIVE"] = live
        app.state.services["LIVE"] = service
        try:
            for runtime in app.state.test_runtimes.values():
                client.portal.call(runtime.close)
            response = client.get("/api/health")
            assert response.status_code == 200
            assert response.json()["status"] == "ok"
            assert response.json()["accountErrors"] == {}
            assert response.json()["availableTestAccountCount"] == 0
            assert response.json()["strategyStatus"] == "PAUSED"
        finally:
            del app.state.runtimes["LIVE"]
            del app.state.services["LIVE"]


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "attention"},
        {"backgroundErrors": {"TEST:5": "runtime_faulted"}},
        {"accountErrors": {"TEST:5": "runtime_faulted"}},
        {"availableTestAccountCount": 9},
        {"testAccountCount": 9},
        {},
    ],
)
def test_container_acceptance_checks_all_accounts_even_after_http_200(monkeypatch, changes):
    state = {
        "revision": "controlled",
        "status": "ok",
        "backgroundErrors": {},
        "accountErrors": {},
        "testAccountCount": 10,
        "availableTestAccountCount": 10,
        "strategyStatus": "PAUSED",
        "liveExecutionEnabled": False,
    } | changes

    class CompleteHealthAccepted(Exception):
        pass

    class Public:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, path):
            if path == "/api/health":
                return httpx.Response(200, json=state)
            # Reaching the next original check proves all health gates passed.
            raise CompleteHealthAccepted

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: Public())
    monkeypatch.setattr(
        sys, "argv", ["container_check.py", "first", "--expected-revision", "controlled"]
    )
    expected = AssertionError if changes else CompleteHealthAccepted
    with pytest.raises(expected):
        runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "container_check.py"))
