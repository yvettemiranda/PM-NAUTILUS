from fastapi.testclient import TestClient

from pm_nautilus.app import create_app


def test_locked_live_history_and_cross_mode_status_are_explicit(tmp_path):
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        test = client.get("/api/dashboard?mode=TEST").json()
        live = client.get("/api/dashboard?mode=LIVE").json()
        records = client.get("/api/LIVE/trade-records").json()
        performance = client.get("/api/LIVE/performance").json()

    assert test["liveStrategyStatus"] == "LOCKED"
    assert live["liveStrategyStatus"] == "LOCKED"
    assert records == {"records": [], "totalCount": 0, "locked": True}
    assert performance["locked"] is True
