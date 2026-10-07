import json
import sqlite3
from types import SimpleNamespace

from eth_account import Account
from fastapi.testclient import TestClient

from pm_nautilus.app import create_app


KEY = "0x" + "1" * 64
ADDRESS = Account.from_key(KEY).address


def test_redemption_approval_requires_its_own_explicit_wallet_action(tmp_path, monkeypatch):
    starts = []

    def settings(material, funder, **_kwargs):
        return {
            "private_key": material.private_key,
            "api_key": "test-key",
            "api_secret": "test-secret",
            "passphrase": "test-passphrase",
            "funder": funder,
            "rpc_url": "https://polygon.example",
            "signature_type": 0,
            "auto_approve_redemption": False,
        }

    class Approval:
        def __init__(self, _wallet, _ledger):
            pass

        def start(self):
            starts.append(True)
            return {"status": "PENDING"}

    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", settings)
    monkeypatch.setattr("pm_nautilus.redemption.PolygonWallet", lambda _settings: object())
    monkeypatch.setattr("pm_nautilus.redemption.RedemptionApprovalService", Approval)
    monkeypatch.setattr(
        "pm_nautilus.app.run_readiness",
        lambda *_args, **_kwargs: {
            "ready": False,
            "checks": [],
            "redemptionApproval": {"status": "MISSING"},
        },
    )

    app = create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        csrf = {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}
        route = "/api/live/wallet/approve-redemption"
        assert client.post(route, json={"confirmation": "APPROVE REDEMPTION"}).status_code == 403
        assert (
            client.post(
                route, json={"confirmation": "APPROVE REDEMPTION"}, headers=csrf
            ).status_code
            == 400
        )
        imported = client.post(
            "/api/live/wallet/import",
            json={
                "kind": "private_key",
                "secret": KEY,
                "publicAddress": ADDRESS,
                "vaultPassword": "my-strong-wallet-password",
            },
            headers=csrf,
        )
        assert imported.status_code == 200, imported.text
        assert starts == []
        assert client.post(route, json={"confirmation": "WRONG"}, headers=csrf).status_code == 400
        assert starts == []
        app.state.runtimes["LIVE"] = SimpleNamespace(status="PAUSED")
        assert (
            client.post(
                route, json={"confirmation": "APPROVE REDEMPTION"}, headers=csrf
            ).status_code
            == 400
        )
        app.state.runtimes.pop("LIVE")
        with sqlite3.connect(tmp_path / "LIVE" / "state.sqlite") as db:
            business = json.loads(
                db.execute("SELECT value FROM meta WHERE key='business'").fetchone()[0]
            )
            business["claims"]["existing"] = {"state": "SUBMITTED"}
            db.execute("UPDATE meta SET value=? WHERE key='business'", (json.dumps(business),))
        assert (
            client.post(
                route, json={"confirmation": "APPROVE REDEMPTION"}, headers=csrf
            ).status_code
            == 400
        )
        with sqlite3.connect(tmp_path / "LIVE" / "state.sqlite") as db:
            business["claims"].clear()
            db.execute("UPDATE meta SET value=? WHERE key='business'", (json.dumps(business),))
        assert starts == []
        response = client.post(route, json={"confirmation": "APPROVE REDEMPTION"}, headers=csrf)
        assert response.status_code == 200, response.text
        assert starts == [True]
        assert response.json()["enabled"] is False
        assert client.get("/api/health").json()["liveExecutionEnabled"] is False
