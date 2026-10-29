"""Optional public cache failures stay local to their simulation account."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import pm_nautilus.app as app_module
from test_token_storage import token


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("PM_UI_PASSWORD", raising=False)


def retention_task(app):
    endpoint = next(
        route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/health"
    )
    cells = dict(zip(endpoint.__code__.co_freevars, endpoint.__closure__))
    return cells["retention_task"].cell_contents


def financial_state(app):
    return {
        number: {
            "status": runtime.status,
            "faulted": runtime.faulted,
            "cash": runtime.cash(),
            "business": runtime.store.get("business"),
            "intents": runtime.store.intents(),
            "events": runtime.store.events(),
            "orders": runtime.native.cache.orders(),
        }
        for number, runtime in app.state.test_runtimes.items()
    }


@pytest.mark.parametrize("failed_number", [1, 5, 10])
def test_failed_test_prune_does_not_pause_or_skip_other_accounts_and_retry_clears_health(
    tmp_path, monkeypatch, failed_number
):
    app = app_module.create_app(tmp_path, public_data=False)
    prune = app_module.prune_cold_metadata
    state = {"fail": True, "called": []}
    with TestClient(app) as client:
        owners = dict(app.state.test_runtimes)
        numbers = {id(runtime): number for number, runtime in owners.items()}

        def prepare():
            # Enter real WARNING maintenance policy with safe write headroom.
            app.state.storage_guard._state = "WARNING"
            for number, runtime in owners.items():
                runtime.add_tokens([token(f"cold-{number}", open=False, ends_ns=1)])
                runtime.start()  # Closed public metadata cannot create a simulated BUY.
            return financial_state(app)

        before = client.portal.call(prepare)
        assert {item["status"] for item in before.values()} == {"RUNNING"}

        def controlled_prune(runtime, *args, **kwargs):
            number = numbers[id(runtime)]
            state["called"].append(number)
            if state["fail"] and number == failed_number:
                raise RuntimeError("private simulated cache-maintenance exception")
            return prune(runtime, *args, **kwargs)

        monkeypatch.setattr(app_module, "prune_cold_metadata", controlled_prune)
        client.portal.call(app.state.maintain_public_metadata_once)
        assert state["called"] == list(range(1, 11))
        assert client.portal.call(financial_state, app) == before
        assert not retention_task(app).done()

        def remaining_tokens():
            return {
                number: (
                    f"cold-{number}" in runtime.tokens,
                    f"cold-{number}" in runtime.store.tokens(),
                )
                for number, runtime in owners.items()
            }

        assert client.portal.call(remaining_tokens) == {
            number: (number == failed_number, number == failed_number) for number in owners
        }
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        key = app.state.test_accounts.key(failed_number)
        assert data["status"] == "attention"
        assert data["accountErrors"] == {key: "metadata_maintenance_error"}
        assert data["backgroundErrors"] == data["accountErrors"]
        assert data["availableTestAccountCount"] == 9
        assert "private simulated" not in response.text
        retention = data["storage"]["metadataRetention"]
        assert retention[key]["status"] == "error"
        assert retention[key]["error"] == "RuntimeError"
        assert retention[key]["removed"] == 0
        for number in owners:
            if number != failed_number:
                item = retention[app.state.test_accounts.key(number)]
                assert item["status"] == "checked" and item["removed"] == 1
                assert "error" not in item

        state.update(fail=False, called=[])
        client.portal.call(app.state.maintain_public_metadata_once)
        assert state["called"] == list(range(1, 11))
        assert client.portal.call(remaining_tokens) == dict.fromkeys(owners, (False, False))
        assert client.portal.call(financial_state, app) == before
        assert not retention_task(app).done()
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["accountErrors"] == data["backgroundErrors"] == {}
        assert data["availableTestAccountCount"] == 10
        for item in data["storage"]["metadataRetention"].values():
            assert item["status"] == "checked" and item["removed"] == 1
            assert "error" not in item


def test_live_maintenance_exception_keeps_original_propagation(tmp_path, monkeypatch):
    app = app_module.create_app(tmp_path, public_data=False)
    prune = app_module.prune_cold_metadata
    with TestClient(app) as client:
        # An in-memory identity proves the LIVE branch without opening a wallet,
        # constructing a private executor, signing, or making network requests.
        live = SimpleNamespace(mode="LIVE")
        app.state.runtimes["LIVE"] = live
        app.state.services["LIVE"] = SimpleNamespace(scan_status={})
        called = []

        def controlled_prune(runtime, *args, **kwargs):
            called.append(runtime)
            if runtime is live:
                raise RuntimeError("controlled LIVE maintenance error")
            return prune(runtime, *args, **kwargs)

        monkeypatch.setattr(app_module, "prune_cold_metadata", controlled_prune)
        app.state.storage_guard._state = "WARNING"
        try:
            with pytest.raises(RuntimeError, match="controlled LIVE maintenance error"):
                client.portal.call(app.state.maintain_public_metadata_once)
            assert called == [app.state.test_runtimes[1], live]
        finally:
            del app.state.runtimes["LIVE"]
            del app.state.services["LIVE"]
