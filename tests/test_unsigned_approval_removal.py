import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from pm_nautilus.app import create_app
from pm_nautilus.store import Store
from test_live_wallet_api import _csrf, _payload, _settings


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "identity",
        "version",
        "missing_pending",
        "pending",
        "remaining",
        "duplicate",
        "extra",
        "receipt",
        "intent",
        "cycle",
    ],
)
def test_only_explicit_authenticated_empty_wallet_removal_cancels_unsigned_preparation(
    tmp_path, monkeypatch, case
):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    monkeypatch.setattr(
        "pm_nautilus.app.run_readiness", lambda *_a, **_k: {"ready": False, "checks": []}
    )
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        assert (
            client.post(
                "/api/live/wallet/import", json=_payload(), headers=_csrf(client)
            ).status_code
            == 200
        )
    store = Store(tmp_path / "LIVE" / "state.sqlite", "LIVE")
    metadata = store.get("live_wallet_identity")
    record = {
        "version": 1,
        "status": "ACTIVE",
        "identity": {
            "signer": metadata["signer"],
            "funder": metadata["funder"],
            "signatureType": metadata["signature_type"],
        },
        "remaining": [False, True],
        "pending": None,
    }
    if case == "identity":
        record["identity"]["funder"] = "0x" + "2" * 40
    elif case == "version":
        record["version"] = True
    elif case == "missing_pending":
        record.pop("pending")
    elif case == "pending":
        record["pending"] = {"raw_tx": "0xab", "tx_hash": "0x" + "a" * 64}
    elif case == "remaining":
        record["remaining"] = [0, True]
    elif case == "duplicate":
        record["remaining"] = [False, False]
    elif case == "extra":
        record["raw_tx"] = "0xab"
    elif case == "receipt":
        record["stage"] = "receipt"
    elif case == "intent":
        store.save_intent(
            "old", {"event_id": "e", "token_id": "1", "side": "BUY", "terminal": True}
        )
    elif case == "cycle":
        business = store.get("business")
        business["cycles"]["old"] = {"quantity": 0, "token_id": "1"}
        store.put("business", business)
    store.put("redemption_approval", record)
    floor_key = f"polygon_nonce_floor:{metadata['signer'].lower()}"
    store.put(floor_key, 9)
    before_generation = store.generation
    before_preferences = store.get("preferences")
    store.close()

    with TestClient(create_app(tmp_path, public_data=False)) as client:
        wallet = client.get("/api/live/wallet").json()
        assert wallet["replaceable"] is (case == "valid")
        payload = {
            "vaultPassword": _payload()["vaultPassword"],
            "confirmation": "REPLACE EMPTY WALLET",
        }
        csrf = _csrf(client)
        if case == "valid":
            for override in ({"vaultPassword": "wrong-password"}, {"confirmation": "WRONG"}):
                assert (
                    client.post(
                        "/api/live/wallet/remove", json={**payload, **override}, headers=csrf
                    ).status_code
                    == 400
                )
                assert (tmp_path / "wallet" / "live.vault").is_file()
                with sqlite3.connect(tmp_path / "LIVE" / "state.sqlite") as db:
                    assert (
                        json.loads(
                            db.execute(
                                "SELECT value FROM meta WHERE key='redemption_approval'"
                            ).fetchone()[0]
                        )
                        == record
                    )
        removed = client.post("/api/live/wallet/remove", json=payload, headers=csrf)
        assert removed.status_code == (200 if case == "valid" else 400), removed.text
        assert client.get("/api/health").json()["liveExecutionEnabled"] is False
    store = Store(tmp_path / "LIVE" / "state.sqlite", "LIVE")
    try:
        assert store.generation == before_generation
        assert store.get("preferences") == before_preferences
        assert store.get(floor_key) == 9
        if case == "valid":
            assert store.get("redemption_approval") is None
            assert store.get("live_wallet_identity") is None
            assert not (tmp_path / "wallet" / "live.vault").exists()
        else:
            assert store.get("redemption_approval") == record
            assert store.get("live_wallet_identity") == metadata
            assert (tmp_path / "wallet" / "live.vault").is_file()
    finally:
        store.close()
