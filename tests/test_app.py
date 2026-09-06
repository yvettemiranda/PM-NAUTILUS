from fastapi.testclient import TestClient
from pm_nautilus.app import create_app


def test_api_isolation_controls_and_reset(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    app = create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        response = client.get("/api/dashboard")
        assert response.status_code == 200
        d = response.json()
        assert d["strategy"]["status"] == "PAUSED"
        assert d["portfolio"]["totalFunds"] == "100"
        csrf = {"x-pm-csrf": response.headers["x-pm-csrf"]}
        assert client.post("/api/test/start").status_code == 403
        assert client.post("/api/test/start", headers=csrf).status_code == 200
        body = {"confirmation": "RESET TEST", "finalConfirmation": "RESET TEST AGAIN"}
        assert client.post("/api/test/reset", json=body, headers=csrf).status_code == 400
        assert client.post("/api/test/pause", headers=csrf).status_code == 200
        assert (
            client.put("/api/live/preferences", json={"orderAmount": "2"}, headers=csrf).status_code
            == 200
        )
        assert client.post("/api/live/start", headers=csrf).status_code == 400
        assert (
            client.put(
                "/api/live/preferences", json={"initialCapital": 100}, headers=csrf
            ).status_code
            == 400
        )
        assert client.post("/api/test/reset", json=body, headers=csrf).status_code == 200
        assert client.get("/api/dashboard?mode=LIVE").json()["preferences"]["orderAmount"] == "2"
        assert client.get("/api/dashboard").json()["preferences"]["orderAmount"] == "1"
        assert client.get("/").status_code == 200
        assert client.get("/api/test/validation").json()["ok"]


def test_auth_origin_and_host(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_UI_PASSWORD", "a-long-local-test-password")
    with TestClient(create_app(tmp_path, public_data=False)) as c:
        assert c.get("/api/dashboard").status_code == 401
        auth = ("pm", "a-long-local-test-password")
        result = c.get("/api/dashboard", auth=auth)
        assert result.status_code == 200
        assert (
            c.post(
                "/api/test/start",
                auth=auth,
                headers={
                    "x-pm-csrf": result.headers["x-pm-csrf"],
                    "origin": "https://evil.example",
                },
            ).status_code
            == 403
        )
        assert c.get("/", auth=auth, headers={"host": "evil.example"}).status_code == 400
