"""Automatic existing-Safe imports; all profile/RPC/CLOB calls are controlled."""

import sqlite3
import json

import pytest
from fastapi.testclient import TestClient

from pm_nautilus import live_web_setup as setup
from pm_nautilus.app import create_app
from pm_nautilus.wallet_vault import read_vault
from test_live_wallet_api import ADDRESS, KEY, _payload
from test_live_web_setup import (
    DiscoveryHTTP,
    DiscoveryRPC,
    SAFE,
    client_factory,
    verified_wallet,
)


def controlled_imports(monkeypatch, signature_type):
    calls = []
    build = setup.build_live_settings

    def prepare(material, funder, **kwargs):
        return build(
            material,
            funder,
            **kwargs,
            inspect_wallet=verified_wallet(signature_type, calls),
            client_factory=client_factory(calls),
            http_client=object(),
        )

    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("PM_POLYGON_RPC_URL", raising=False)
    monkeypatch.setattr(setup, "build_live_settings", prepare)
    monkeypatch.setattr(
        "pm_nautilus.app.run_readiness", lambda *_args, **_kwargs: {"ready": False, "checks": []}
    )
    return calls


@pytest.mark.parametrize("blank", ["", "   ", None, "omitted"])
def test_blank_address_import_discovers_and_rechecks_safe_without_trading(
    tmp_path, monkeypatch, blank
):
    calls = controlled_imports(monkeypatch, 2)
    discover = setup.discover_funder
    transport, rpc = DiscoveryHTTP(), DiscoveryRPC()
    monkeypatch.setattr(
        setup,
        "discover_funder",
        lambda signer, **kwargs: discover(
            signer, **kwargs, http_client=transport, rpc_factory=lambda *_args: rpc
        ),
    )
    payload = {**_payload(), "publicAddress": blank}
    if blank == "omitted":
        payload.pop("publicAddress")
    app = create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        csrf = {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}
        result = client.post("/api/live/wallet/import", json=payload, headers=csrf)
        assert result.status_code == 200, result.text
        wallet = result.json()
        assert wallet["signer"] == ADDRESS and wallet["funder"] == SAFE
        assert wallet["signatureType"] == 2
        assert wallet["enabled"] is False and wallet["readiness"]["ready"] is False
        assert set(app.state.runtimes) == {"TEST"}
        assert client.get("/api/health").json()["liveExecutionEnabled"] is False
        assert KEY not in result.text and payload["vaultPassword"] not in result.text
        assert [call[0] for call in calls] == ["inspect", "client", "derive"]
        assert transport.requests[0][1] == {"address": ADDRESS}
        with sqlite3.connect(tmp_path / "LIVE" / "state.sqlite") as db:
            identity = json.loads(
                db.execute("SELECT value FROM meta WHERE key='live_wallet_identity'").fetchone()[0]
            )
        assert identity == {"signer": ADDRESS, "funder": SAFE, "signature_type": 2}
    saved = read_vault(tmp_path / "wallet" / "live.vault", payload["vaultPassword"])
    assert saved["private_key"] == KEY and saved["funder"] == SAFE
    assert saved["signature_type"] == 2 and saved["auto_approve_redemption"] is False


def test_explicit_address_keeps_existing_eoa_semantics_without_discovery(tmp_path, monkeypatch):
    calls = controlled_imports(monkeypatch, 0)
    monkeypatch.setattr(
        setup, "discover_funder", lambda *_args, **_kwargs: pytest.fail("No discovery")
    )
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        csrf = {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}
        result = client.post("/api/live/wallet/import", json=_payload(), headers=csrf)
        assert result.status_code == 200, result.text
        assert result.json()["funder"] == ADDRESS and result.json()["signatureType"] == 0
        assert result.json()["enabled"] is False
        assert [call[0] for call in calls] == ["inspect", "client", "derive"]


@pytest.mark.parametrize("failure", ["profile", "factory", "owner", "network"])
def test_failed_discovery_never_derives_auth_or_creates_vault(tmp_path, monkeypatch, failure):
    calls = controlled_imports(monkeypatch, 2)
    discover = setup.discover_funder
    transport = DiscoveryHTTP({} if failure == "profile" else {"proxyWallet": SAFE})
    if failure == "network":
        transport.error = RuntimeError("provider secret must not leak")
    rpc = DiscoveryRPC(
        **(
            {"expected": "0x" + "33" * 20}
            if failure == "factory"
            else {"owners": ["0x" + "33" * 20]}
            if failure == "owner"
            else {}
        )
    )
    monkeypatch.setattr(
        setup,
        "discover_funder",
        lambda signer, **kwargs: discover(
            signer, **kwargs, http_client=transport, rpc_factory=lambda *_args: rpc
        ),
    )
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        csrf = {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}
        result = client.post(
            "/api/live/wallet/import", json={**_payload(), "publicAddress": ""}, headers=csrf
        )
        assert result.status_code == 400
        assert "provider secret" not in result.text and KEY not in result.text
        assert not (tmp_path / "wallet" / "live.vault").exists()
        assert calls == []
        assert client.get("/api/live/wallet").json()["configured"] is False
        assert client.get("/api/health").json()["liveExecutionEnabled"] is False


def test_discovered_safe_cannot_bypass_existing_ledger_identity(tmp_path, monkeypatch):
    calls = controlled_imports(monkeypatch, 2)
    monkeypatch.setattr(setup, "discover_funder", lambda *_args, **_kwargs: SAFE)
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        with sqlite3.connect(tmp_path / "LIVE" / "state.sqlite") as db:
            db.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('live_wallet_identity',?)",
                (json.dumps({"signer": ADDRESS, "funder": ADDRESS, "signature_type": 0}),),
            )
        csrf = {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}
        result = client.post(
            "/api/live/wallet/import", json={**_payload(), "publicAddress": ""}, headers=csrf
        )
        assert result.status_code == 400
        assert calls == [] and not (tmp_path / "wallet" / "live.vault").exists()
