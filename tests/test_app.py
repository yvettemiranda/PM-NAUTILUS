import json
import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient
from pm_nautilus.app import create_app
from pm_nautilus.config import OPTIONAL_PROFILE_FIELDS
from pm_nautilus.store import Store


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


def test_legacy_preferences_are_read_only_and_new_flags_are_isolated(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("PM_UI_PASSWORD", raising=False)

    def raw_preferences(path):
        with closing(sqlite3.connect(path)) as db:
            return db.execute("SELECT value FROM meta WHERE key='preferences'").fetchone()[0]

    original = {}
    for mode, order_amount in (("TEST", "2"), ("LIVE", "7")):
        path = tmp_path / mode / "state.sqlite"
        with closing(Store(path, mode)) as store:
            previous = store.get("preferences")
            assert len(previous) == 15
            assert not OPTIONAL_PROFILE_FIELDS.intersection(previous)
            store.put("preferences", previous | {"orderAmount": order_amount})
        original[path] = raw_preferences(path)

    disabled = dict.fromkeys(OPTIONAL_PROFILE_FIELDS, False)
    enabled = dict.fromkeys(OPTIONAL_PROFILE_FIELDS, True)

    def read_flags(client, route, expected):
        response = client.get(route)
        assert response.status_code == 200
        preferences = response.json()["preferences"]
        assert {key: preferences[key] for key in expected} == expected
        return response

    test_first = "/api/test/preferences?account_id=1"
    test_second = "/api/test/preferences?account_id=2"
    live = "/api/live/preferences"
    live_flags = disabled | {"buyBestLevelOnly": True}
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        response = read_flags(client, test_first, disabled)
        assert response.json()["preferences"]["orderAmount"] == "2"
        assert read_flags(client, live, disabled).json()["preferences"]["orderAmount"] == "7"
        assert {path: raw_preferences(path) for path in original} == original
        csrf = {"x-pm-csrf": response.headers["x-pm-csrf"]}
        for route in (test_first, live):
            assert client.put(route, json=disabled, headers=csrf).status_code == 200
        assert {path: raw_preferences(path) for path in original} == original
        assert client.put(test_second, json=enabled, headers=csrf).status_code == 200
        assert client.put(live, json={"buyBestLevelOnly": True}, headers=csrf).status_code == 200
        assert client.put(live, json={"cyclePerMarket": "true"}, headers=csrf).status_code == 400
        read_flags(client, test_first, disabled)
        read_flags(client, test_second, enabled)
        read_flags(client, live, live_flags)

    with TestClient(create_app(tmp_path, public_data=False)) as client:
        read_flags(client, test_first, disabled)
        read_flags(client, test_second, enabled)
        read_flags(client, live, live_flags)
    first = tmp_path / "TEST" / "state.sqlite"
    assert raw_preferences(first) == original[first]
    persisted_live = json.loads(raw_preferences(tmp_path / "LIVE" / "state.sqlite"))
    assert OPTIONAL_PROFILE_FIELDS.intersection(persisted_live) == {"buyBestLevelOnly"}
    assert persisted_live["buyBestLevelOnly"] is True
