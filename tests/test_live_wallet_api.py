"""Web onboarding must not expose secrets or silently start LIVE."""

import importlib
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

from eth_account import Account
from fastapi.testclient import TestClient

from pm_nautilus.app import create_app
from pm_nautilus.store import Store


KEY = "0x" + "1" * 64
ADDRESS = Account.from_key(KEY).address


def _settings(material, funder, **_kwargs):
    return {
        "private_key": material.private_key,
        "api_key": "api-key-test",
        "api_secret": "api-secret-test",
        "passphrase": "api-passphrase-test",
        "funder": funder,
        "rpc_url": "https://polygon.example",
        "signature_type": 0,
        "auto_approve_redemption": False,
    }


def _payload():
    return {
        "kind": "private_key",
        "secret": KEY,
        "publicAddress": ADDRESS,
        "vaultPassword": "my-strong-wallet-password",
    }


def _csrf(client):
    return {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}


def test_wallet_import_is_encrypted_private_and_live_stays_off(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    monkeypatch.setattr(
        "pm_nautilus.app.run_readiness",
        lambda *args, **kwargs: {"ready": True, "checks": []},
    )
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        assert client.get("/api/live/wallet").json()["status"] == "UNCONFIGURED"
        assert client.post("/api/live/wallet/import", json=_payload()).status_code == 403
        response = client.post("/api/live/wallet/import", json=_payload(), headers=_csrf(client))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "READY"
        assert body["enabled"] is False
        assert body["signer"] == ADDRESS
        assert KEY not in response.text
        assert _payload()["vaultPassword"] not in response.text
        assert client.get("/api/dashboard?mode=LIVE").json()["positionsAvailable"] is False
        assert client.get("/api/dashboard?mode=TEST").json()["executionMode"] == "TEST"
        assert client.post("/api/live/start", headers=_csrf(client)).status_code == 400
    vault = tmp_path / "wallet" / "live.vault"
    assert stat.S_IMODE(vault.stat().st_mode) == 0o600
    assert KEY.encode() not in vault.read_bytes()


def test_reboot_requires_web_unlock_and_blocked_check_cannot_enable(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    monkeypatch.setattr(
        "pm_nautilus.app.run_readiness",
        lambda *args, **kwargs: {
            "ready": False,
            "checks": [{"id": "region", "status": "blocked", "message": "区域受限"}],
        },
    )
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        response = client.post("/api/live/wallet/import", json=_payload(), headers=_csrf(client))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "BLOCKED"
        assert (
            client.post(
                "/api/live/wallet/enable",
                json={"confirmation": "ENABLE LIVE"},
                headers=_csrf(client),
            ).status_code
            == 400
        )
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        assert client.get("/api/live/wallet").json()["status"] == "LOCKED"
        wrong = client.post(
            "/api/live/wallet/unlock",
            json={"vaultPassword": "wrong-password"},
            headers=_csrf(client),
        )
        assert wrong.status_code == 400
        response = client.post(
            "/api/live/wallet/unlock",
            json={"vaultPassword": _payload()["vaultPassword"]},
            headers=_csrf(client),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "BLOCKED"
        assert client.get("/api/dashboard?mode=TEST").status_code == 200


def test_remote_wallet_import_requires_https(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("PM_TRUST_HTTPS_PROXY", raising=False)
    monkeypatch.setenv("PM_ALLOWED_HOSTS", "example.com")
    monkeypatch.setenv("PM_UI_PASSWORD", "a-long-local-test-password")
    with TestClient(
        create_app(tmp_path, public_data=False), base_url="http://example.com"
    ) as client:
        auth = ("pm", "a-long-local-test-password")
        headers = {"x-pm-csrf": client.get("/api/dashboard", auth=auth).headers["x-pm-csrf"]}
        response = client.post(
            "/api/live/wallet/import", auth=auth, json=_payload(), headers=headers
        )
        assert response.status_code == 403
        assert not (tmp_path / "wallet" / "live.vault").exists()


def test_empty_wallet_can_be_replaced_in_the_web_page(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    monkeypatch.setattr(
        "pm_nautilus.app.run_readiness",
        lambda *args, **kwargs: {"ready": False, "checks": []},
    )
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        response = client.post("/api/live/wallet/import", json=_payload(), headers=_csrf(client))
        assert response.status_code == 200
        assert response.json()["replaceable"] is True
        remove = {"vaultPassword": "wrong-password", "confirmation": "REPLACE EMPTY WALLET"}
        assert (
            client.post("/api/live/wallet/remove", json=remove, headers=_csrf(client)).status_code
            == 400
        )
        assert (tmp_path / "wallet" / "live.vault").exists()
        remove["vaultPassword"] = _payload()["vaultPassword"]
        response = client.post("/api/live/wallet/remove", json=remove, headers=_csrf(client))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "UNCONFIGURED"
        assert not (tmp_path / "wallet" / "live.vault").exists()
        assert (
            client.post(
                "/api/live/wallet/import", json=_payload(), headers=_csrf(client)
            ).status_code
            == 200
        )
        store = Store(tmp_path / "LIVE" / "state.sqlite", "LIVE")
        store.save_intent(
            "already", {"event_id": "e", "token_id": "1", "side": "BUY", "terminal": True}
        )
        store.close()
        assert client.get("/api/live/wallet").json()["replaceable"] is False
        assert (
            client.post("/api/live/wallet/remove", json=remove, headers=_csrf(client)).status_code
            == 400
        )


def test_web_backup_restores_wallet_and_live_rules_on_fresh_server(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    monkeypatch.setattr(
        "pm_nautilus.app.run_readiness",
        lambda *args, **kwargs: {"ready": True, "checks": []},
    )
    source = tmp_path / "source"
    with TestClient(create_app(source, public_data=False)) as client:
        assert (
            client.post(
                "/api/live/wallet/import", json=_payload(), headers=_csrf(client)
            ).status_code
            == 200
        )
        assert (
            client.put(
                "/api/live/preferences", json={"orderAmount": "2"}, headers=_csrf(client)
            ).status_code
            == 200
        )
        response = client.post(
            "/api/live/wallet/backup",
            json={"vaultPassword": _payload()["vaultPassword"]},
            headers=_csrf(client),
        )
        assert response.status_code == 200, response.text[:200]
        bundle = response.content
        assert KEY.encode() not in bundle

    destination = tmp_path / "destination"
    with TestClient(create_app(destination, public_data=False)) as client:
        password = _payload()["vaultPassword"].encode("utf-8")
        response = client.post(
            "/api/live/wallet/restore",
            content=len(password).to_bytes(2, "big") + password + bundle,
            headers=_csrf(client) | {"content-type": "application/octet-stream"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "LOCKED"
        assert client.get("/api/dashboard?mode=LIVE").json()["preferences"]["orderAmount"] == "2"
        assert client.post("/api/live/start", headers=_csrf(client)).status_code == 400
        response = client.post(
            "/api/live/wallet/unlock",
            json={"vaultPassword": _payload()["vaultPassword"]},
            headers=_csrf(client),
        )
        assert response.status_code == 200, response.text
        assert response.json()["funder"] == ADDRESS


def test_enable_attaches_paused_and_requires_separate_start(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    monkeypatch.setattr(
        "pm_nautilus.app.run_readiness",
        lambda *args, **kwargs: {"ready": True, "checks": []},
    )
    app_module = importlib.import_module("pm_nautilus.app")
    app = create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/live/wallet/import", json=_payload(), headers=_csrf(client)
            ).status_code
            == 200
        )
        start_calls = []
        activation_entered = threading.Event()
        activation_release = threading.Event()

        class FakeClient:
            async def activate(self):
                import asyncio

                activation_entered.set()
                assert await asyncio.to_thread(activation_release.wait, 5)

            async def shutdown(self):
                pass

        class FakeRuntime:
            def __init__(self, path, mode, live_factory):
                self.store = Store(path, mode)
                self.mode = mode
                self.client = FakeClient()
                self.faulted = None

            @property
            def status(self):
                return self.store.get("status")

            def pause(self):
                self.store.put("status", "PAUSED")

        class FakeService:
            def __init__(self, runtime):
                self.runtime = runtime
                self.loop_task = None

            def start(self):
                start_calls.append(True)
                self.runtime.store.put("status", "RUNNING")

            async def close(self):
                pass

        class FakeSampler:
            def __init__(self, runtime):
                self.runtime = runtime

            async def run(self):
                import asyncio

                await asyncio.Event().wait()

        class FakeWallet:
            def __init__(self, settings):
                pass

            def preflight(self):
                pass

        class FakeRedemption:
            def __init__(self, *args):
                pass

            async def run_once(self, *args):
                pass

        monkeypatch.setattr(app_module, "Runtime", FakeRuntime)
        monkeypatch.setattr(app_module, "MarketService", FakeService)
        monkeypatch.setattr(app_module, "RedemptionService", FakeRedemption)
        monkeypatch.setattr(app_module, "PerformanceSampler", FakeSampler)
        monkeypatch.setattr(
            app_module, "dashboard", lambda runtime, *_: {"strategy": {"status": runtime.status}}
        )
        monkeypatch.setattr("pm_nautilus.redemption.PolygonWallet", FakeWallet)
        monkeypatch.setattr("pm_nautilus.live.live_factory", lambda *args: None)

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                client.post,
                "/api/live/wallet/enable",
                json={"confirmation": "ENABLE LIVE"},
                headers=_csrf(client),
            )
            assert activation_entered.wait(5)
            assert "LIVE" not in app.state.runtimes
            assert client.get("/api/dashboard?mode=LIVE").status_code == 200
            assert client.get("/api/live/wallet").json()["enabled"] is False
            activation_release.set()
            response = pending.result(timeout=5)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "PAUSED"
        assert not start_calls
        response = client.post("/api/live/start", headers=_csrf(client))
        assert response.status_code == 200, response.text
        assert response.json()["wallet"]["status"] == "RUNNING"
        assert len(start_calls) == 1
