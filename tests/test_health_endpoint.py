"""Health reflects fatal runtime and LIVE supervision state without secret details."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from pm_nautilus.app import create_app


def test_health_reports_runtime_faults_and_recovers(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    app = create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        runtime = app.state.runtimes["TEST"]
        assert client.get("/api/health").status_code == 200

        runtime.faulted = "private RPC URL or exception detail"
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["backgroundErrors"] == {"TEST": "runtime_faulted"}
        assert "private" not in response.text

        runtime.faulted = None
        runtime.business["recovery_error"] = "private account detail"
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["backgroundErrors"] == {"TEST": "recovery_error"}
        assert "private" not in response.text

        runtime.business["recovery_error"] = None
        assert client.get("/api/health").status_code == 200


def test_health_reports_live_account_and_maintenance_faults(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    app = create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        account = {"live_error": None}
        live = SimpleNamespace(
            faulted=None,
            business={"recovery_error": None},
            store=SimpleNamespace(get=lambda key: account.get(key)),
            client=SimpleNamespace(reconciliation_task=SimpleNamespace(done=lambda: False)),
        )
        market = SimpleNamespace(scan_status={}, loop_task=None, closed=False)
        app.state.runtimes["LIVE"] = live
        app.state.services["LIVE"] = market
        try:
            # An ordinary PAUSED service with a running maintenance task is healthy.
            assert client.get("/api/health").status_code == 200

            account["live_error"] = "private account response"
            response = client.get("/api/health")
            assert response.status_code == 503
            assert response.json()["backgroundErrors"] == {"LIVE": "live_account_error"}
            assert "private" not in response.text

            account["live_error"] = None
            live.client.reconciliation_task = None
            response = client.get("/api/health")
            assert response.status_code == 503
            assert response.json()["backgroundErrors"] == {"LIVE": "live_maintenance_task_stopped"}

            live.client.reconciliation_task = SimpleNamespace(done=lambda: True)
            assert client.get("/api/health").status_code == 503
            live.client.reconciliation_task = SimpleNamespace(done=lambda: False)
            assert client.get("/api/health").status_code == 200
        finally:
            del app.state.services["LIVE"]
            del app.state.runtimes["LIVE"]
