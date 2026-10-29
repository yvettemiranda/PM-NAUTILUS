"""Health reflects fatal runtime and LIVE supervision state without secret details."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from pm_nautilus.app import create_app
from pm_nautilus.redemption import RedemptionService

from test_market_availability_views import unavailable
from test_runtime import setup


def test_health_reports_runtime_faults_and_recovers(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    app = create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        runtime = app.state.runtimes["TEST"]
        assert client.get("/api/health").status_code == 200

        runtime.faulted = "private RPC URL or exception detail"
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "attention"
        assert response.json()["backgroundErrors"] == {"TEST": "runtime_faulted"}
        assert response.json()["accountErrors"] == {"TEST": "runtime_faulted"}
        assert "private" not in response.text

        runtime.faulted = None
        runtime.business["recovery_error"] = "private account detail"
        response = client.get("/api/health")
        assert response.status_code == 200
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
        market = SimpleNamespace(
            scan_status={}, loop_task=None, closed=False, stream_diagnostics=lambda: {"groups": []}
        )
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


def owned_health_app(tmp_path):
    seed, clock, token = setup(tmp_path / "seed")
    seed.close()
    app = create_app(tmp_path / "server", public_data=False, test_clock=clock)
    return app, token


def seed_held_market(app, token):
    runtime = app.state.runtimes["TEST"]
    runtime.add_tokens([token])
    runtime.book(token.token_id, [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    runtime.start()
    runtime.pause()
    runtime.market_availability[token.token_id] = unavailable(token)


def test_health_reports_nonfatal_market_availability_without_disclosing_positions(tmp_path):
    app, token = owned_health_app(tmp_path)
    with TestClient(app) as client:
        client.portal.call(seed_held_market, app, token)
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["backgroundErrors"] == {}
        state = response.json()["marketData"]["TEST"]
        assert state["status"] == "attention"
        assert state["unavailablePositionCount"] == 1 and state["notReadyPositionCount"] == 0
        assert set(state) == {
            "status",
            "unavailablePositionCount",
            "notReadyPositionCount",
            "readyGroupCount",
            "groupCount",
            "readyBookCount",
            "tokenCount",
        }
        assert not {"tokenId", "conditionId", "marketId", "quantity", "availableCash"} & set(state)

        # A recovering token cannot look healthy just because a prior cache is READY.
        def recovering():
            runtime = app.state.runtimes["TEST"]
            runtime.market_availability.clear()
            runtime.market_recovering.add(token.token_id)
            assert runtime.books[token.token_id].ready

        client.portal.call(recovering)
        state = client.get("/api/health").json()["marketData"]["TEST"]
        assert state["status"] == "recovering" and state["notReadyPositionCount"] == 1
        assert state["unavailablePositionCount"] == 0
        position = client.get("/api/dashboard?mode=TEST").json()["positions"][0]
        assert position["currentSellPriceStatus"] == "NOT_READY"
        assert position["currentSellPrice"] is None and position["executableSellValue"] is None


def test_settled_rights_do_not_leave_stale_market_attention_in_public_health(tmp_path):
    app, token = owned_health_app(tmp_path)
    with TestClient(app) as client:
        client.portal.call(seed_held_market, app, token)

        async def settle():
            runtime = app.state.runtimes["TEST"]
            service = RedemptionService(runtime, None)
            service.identify(token, {token.token_id: 1_000_000, "sibling": 0})
            claim = runtime.business["claims"][token.condition_id]
            await service.advance(claim)
            await service.advance(claim)
            assert claim["state"] == "CREDITED" and runtime.validate()["ok"]
            assert token.token_id in runtime.market_availability

        client.portal.call(settle)
        state = client.get("/api/health").json()["marketData"]["TEST"]
        assert state["status"] == "ok"
        assert state["unavailablePositionCount"] == state["notReadyPositionCount"] == 0
