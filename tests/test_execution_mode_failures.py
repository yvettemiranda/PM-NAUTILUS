"""Uncertain mode persistence never resumes trading or dormant TEST tasks."""

import pytest

from pm_nautilus.execution_mode import ExecutionModeStore
from test_execution_modes import enable, wallet_app
from test_live_wallet_api import _csrf


@pytest.mark.parametrize("after_replace", [False, True])
def test_failed_live_mode_write_drains_live_and_keeps_test_stopped(
    tmp_path, monkeypatch, after_replace
):
    original = ExecutionModeStore.save
    with wallet_app(tmp_path, monkeypatch) as (app, client, state):

        def fail(store, mode):
            if mode == "LIVE":
                if after_replace:
                    original(store, mode)
                raise OSError("controlled mode durability failure")
            return original(store, mode)

        monkeypatch.setattr(ExecutionModeStore, "save", fail)
        headers = _csrf(client)
        response = enable(client, headers)
        assert response.status_code == 400
        assert app.state.execution_state["testSuspended"]
        assert app.state.execution_state["persistenceUnconfirmed"]
        assert "LIVE" not in app.state.runtimes
        assert state["live"] == []  # Intent must be durable before private activation.
        assert all(service.closed for service in app.state.test_services.values())
        health = client.get("/api/health")
        assert health.status_code == 503
        assert health.json()["backgroundErrors"]["executionMode"] == "state_unconfirmed"
        restored = client.post(
            "/api/test/restore-background?account_id=10",
            json={"confirmation": "RESTORE TEST"},
            headers=headers,
        )
        assert restored.status_code == 200, restored.text
        assert not app.state.execution_state["persistenceUnconfirmed"]
        assert not app.state.execution_state["testSuspended"]
        assert client.portal.call(
            lambda: {owner.status for owner in app.state.test_runtimes.values()}
        ) == {"PAUSED"}
        assert ExecutionModeStore(tmp_path).read() == "TEST"


def test_failed_enable_does_not_restore_previously_dormant_test(tmp_path, monkeypatch):
    ExecutionModeStore(tmp_path).save("LIVE")
    with wallet_app(tmp_path, monkeypatch) as (app, client, state):

        async def fail(_owner):
            raise ValueError("controlled activation failure")

        state["activate"] = fail
        response = enable(client, _csrf(client))
        assert response.status_code == 400
        assert app.state.execution_state["testSuspended"]
        assert not app.state.execution_state["persistenceUnconfirmed"]
        assert not app.state.execution_state["cleanupUnconfirmed"]
        assert all(service.closed for service in app.state.test_services.values())
        assert not app.state.market_hub.services
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["activeExecutionMode"] == "STOPPED"
        assert ExecutionModeStore(tmp_path).read() == "LIVE"


def test_failed_explicit_restore_stays_dormant_until_mode_is_saved(tmp_path, monkeypatch):
    ExecutionModeStore(tmp_path).save("LIVE")
    with wallet_app(tmp_path, monkeypatch) as (app, client, _state):

        def fail(*_args):
            raise OSError("controlled mode write failure")

        monkeypatch.setattr(ExecutionModeStore, "save", fail)
        response = client.post(
            "/api/test/restore-background?account_id=2",
            json={"confirmation": "RESTORE TEST"},
            headers=_csrf(client),
        )
        assert response.status_code == 400
        assert app.state.execution_state["testSuspended"]
        assert app.state.execution_state["persistenceUnconfirmed"]
        assert not app.state.market_hub.services
        assert all(service.closed for service in app.state.test_services.values())
        assert ExecutionModeStore(tmp_path).read() == "LIVE"


def test_unavailable_selected_account_cannot_restore_other_accounts(tmp_path, monkeypatch):
    ExecutionModeStore(tmp_path).save("LIVE")
    with wallet_app(tmp_path, monkeypatch) as (app, client, _state):
        app.state.test_accounts.contexts[1].error = "controlled account failure"
        response = client.post(
            "/api/test/restore-background?account_id=1",
            json={"confirmation": "RESTORE TEST"},
            headers=_csrf(client),
        )
        assert response.status_code == 503
        assert app.state.execution_state["testSuspended"]
        assert not app.state.market_hub.services
        assert ExecutionModeStore(tmp_path).read() == "LIVE"


def test_private_activation_observes_durable_live_intent_and_rollback_restores_test(
    tmp_path, monkeypatch
):
    with wallet_app(tmp_path, monkeypatch) as (app, client, state):

        async def inspect_then_fail(_owner):
            assert ExecutionModeStore(tmp_path).read() == "LIVE"
            assert app.state.execution_state["testSuspended"]
            raise ValueError("controlled activation failure")

        async def inspect_shutdown(_owner):
            assert ExecutionModeStore(tmp_path).read() == "LIVE"
            assert app.state.execution_state["testSuspended"]

        state.update(activate=inspect_then_fail, shutdown=inspect_shutdown)
        assert enable(client, _csrf(client)).status_code == 400
        assert state["shutdownFinished"]
        assert ExecutionModeStore(tmp_path).read() == "TEST"
        assert not app.state.execution_state["testSuspended"]


def test_failed_rollback_mode_write_keeps_test_stopped(tmp_path, monkeypatch):
    original = ExecutionModeStore.save
    with wallet_app(tmp_path, monkeypatch) as (app, client, state):

        def fail(store, mode):
            if mode == "TEST":
                raise OSError("controlled rollback persistence failure")
            return original(store, mode)

        async def fail_activation(_owner):
            raise ValueError("controlled activation failure")

        monkeypatch.setattr(ExecutionModeStore, "save", fail)
        state["activate"] = fail_activation
        assert enable(client, _csrf(client)).status_code == 400
        assert state["shutdownFinished"]
        assert app.state.execution_state["testSuspended"]
        assert app.state.execution_state["persistenceUnconfirmed"]
        assert not app.state.market_hub.services
        assert ExecutionModeStore(tmp_path).read() == "LIVE"
