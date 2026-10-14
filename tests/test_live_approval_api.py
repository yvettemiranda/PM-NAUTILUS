import json
import sqlite3
from types import SimpleNamespace

import pytest
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


@pytest.mark.parametrize("read_failure", ["exception", "unknown"])
def test_durable_pending_approval_survives_failed_account_refresh(
    tmp_path, monkeypatch, read_failure
):
    from test_live_wallet_api import _payload, _settings
    from test_redemption_approval import ControlledWallet, saved

    wallet = ControlledWallet()
    wallet.signer.address = wallet.funder = ADDRESS
    wallet.signature_type = 0
    wallet.broadcast_fails = True
    reads = 0

    def readiness(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        if reads > 1 and read_failure == "exception":
            raise ConnectionError("controlled private RPC failure must not be public")
        return {
            "ready": False,
            "checks": [{"id": "region", "status": "blocked", "message": "区域受限"}],
            "redemptionApproval": {
                "status": "MISSING" if reads == 1 else "UNKNOWN",
                "standardApproved": False if reads == 1 else None,
                "negRiskApproved": False if reads == 1 else None,
                "pendingTxHash": None,
                "networkError": False,
            },
        }

    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    monkeypatch.setattr("pm_nautilus.redemption.PolygonWallet", lambda _settings: wallet)
    monkeypatch.setattr("pm_nautilus.app.run_readiness", readiness)
    app = create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        csrf = {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}
        assert (
            client.post("/api/live/wallet/import", json=_payload(), headers=csrf).status_code == 200
        )
        response = client.post(
            "/api/live/wallet/approve-redemption",
            json={"confirmation": "APPROVE REDEMPTION"},
            headers=csrf,
        )
        assert response.status_code == 200, response.text
        result = response.json()
        approval = result["redemptionApproval"]
        assert approval["status"] == "PENDING" and approval["networkError"] is True
        assert approval["pendingTxHash"] == wallet.broadcasts[0]
        assert result["readiness"]["ready"] is False and result["status"] == "BLOCKED"
        assert result["enabled"] is False and set(app.state.runtimes) == {"TEST"}
        assert client.get("/api/live/wallet").json()["redemptionApproval"] == approval
        checked = client.post("/api/live/wallet/check", json={}, headers=csrf)
        assert checked.status_code == 200
        assert checked.json()["redemptionApproval"] == approval
        assert checked.json()["readiness"]["ready"] is False
        enabled = client.post(
            "/api/live/wallet/enable", json={"confirmation": "ENABLE LIVE"}, headers=csrf
        )
        assert enabled.status_code == 400
        assert client.get("/api/live/wallet").json()["redemptionApproval"] == approval
        assert client.get("/api/health").json()["liveExecutionEnabled"] is False
        persisted = saved(tmp_path / "LIVE" / "state.sqlite")
        assert persisted["status"] == "ACTIVE"
        assert persisted["pending"]["tx_hash"] == approval["pendingTxHash"]
        assert wallet.prepared == [False] and len(wallet.broadcasts) == 1
        other_key = "0x" + "2" * 64
        monkeypatch.setattr(
            "pm_nautilus.app.read_vault",
            lambda *_args: {
                **_settings(
                    SimpleNamespace(private_key=other_key), Account.from_key(other_key).address
                )
            },
        )
        foreign_unlock = client.post(
            "/api/live/wallet/unlock",
            json={"vaultPassword": _payload()["vaultPassword"]},
            headers=csrf,
        )
        assert foreign_unlock.status_code == 400
        assert client.get("/api/live/wallet").json()["funder"] == ADDRESS
        assert client.get("/api/live/wallet").json()["redemptionApproval"] == approval
        for private_value in (KEY, _payload()["vaultPassword"], persisted["pending"]["raw_tx"]):
            assert private_value not in response.text
        assert "controlled private RPC" not in response.text


def test_replacing_and_restoring_wallets_clear_cached_pending_approval(tmp_path, monkeypatch):
    from test_live_wallet_api import _payload, _settings

    class Approval:
        def __init__(self, _wallet, _ledger):
            pass

        def start(self):
            return {
                "status": "PENDING",
                "standardApproved": False,
                "negRiskApproved": False,
                "pendingTxHash": "0x" + "a" * 64,
                "networkError": True,
            }

    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    monkeypatch.setattr("pm_nautilus.redemption.PolygonWallet", lambda _settings: object())
    monkeypatch.setattr("pm_nautilus.redemption.RedemptionApprovalService", Approval)
    monkeypatch.setattr(
        "pm_nautilus.app.run_readiness", lambda *_args, **_kwargs: {"ready": False, "checks": []}
    )
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        csrf = {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}
        assert (
            client.post("/api/live/wallet/import", json=_payload(), headers=csrf).status_code == 200
        )
        backup = client.post(
            "/api/live/wallet/backup",
            json={"vaultPassword": _payload()["vaultPassword"]},
            headers=csrf,
        )
        assert backup.status_code == 200
        approve = {"confirmation": "APPROVE REDEMPTION"}
        response = client.post("/api/live/wallet/approve-redemption", json=approve, headers=csrf)
        assert response.json()["redemptionApproval"]["status"] == "PENDING"
        # This controlled service only seeds the public cache, with no signed or
        # durable transaction. A real pending transaction forbids replacement.
        remove = {
            "vaultPassword": _payload()["vaultPassword"],
            "confirmation": "REPLACE EMPTY WALLET",
        }
        removed = client.post("/api/live/wallet/remove", json=remove, headers=csrf)
        assert removed.status_code == 200, removed.text
        assert removed.json()["redemptionApproval"] is None
        other_key = "0x" + "2" * 64
        imported = client.post(
            "/api/live/wallet/import",
            json={
                **_payload(),
                "secret": other_key,
                "publicAddress": Account.from_key(other_key).address,
            },
            headers=csrf,
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()["funder"] != ADDRESS
        assert imported.json()["redemptionApproval"]["status"] == "UNKNOWN"
        assert imported.json()["redemptionApproval"]["pendingTxHash"] is None
        assert client.post("/api/live/wallet/remove", json=remove, headers=csrf).status_code == 200
        password = _payload()["vaultPassword"].encode()
        restored = client.post(
            "/api/live/wallet/restore",
            content=len(password).to_bytes(2, "big") + password + backup.content,
            headers=csrf | {"content-type": "application/octet-stream"},
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["status"] == "LOCKED"
        assert restored.json()["redemptionApproval"] is None
        unlocked = client.post(
            "/api/live/wallet/unlock",
            json={"vaultPassword": _payload()["vaultPassword"]},
            headers=csrf,
        )
        assert unlocked.status_code == 200, unlocked.text
        assert unlocked.json()["funder"] == ADDRESS
        assert unlocked.json()["redemptionApproval"]["status"] == "UNKNOWN"
        assert unlocked.json()["redemptionApproval"]["pendingTxHash"] is None


@pytest.mark.parametrize(
    ("known", "fresh", "expected"),
    [
        ({"pendingTxHash": "invalid"}, "UNKNOWN", "UNKNOWN"),
        ({"standardApproved": "false"}, "UNKNOWN", "UNKNOWN"),
        (
            {
                "status": "APPROVED",
                "standardApproved": True,
                "negRiskApproved": True,
                "pendingTxHash": None,
            },
            "UNKNOWN",
            "UNKNOWN",
        ),
        ({}, "APPROVED", "APPROVED"),
        ({}, "MISSING", "MISSING"),
        ({}, "PENDING", "PENDING"),
    ],
)
def test_account_refresh_never_uses_invalid_or_superseded_approval_result(
    tmp_path, monkeypatch, known, fresh, expected
):
    from test_live_wallet_api import _payload, _settings

    pending = {
        "status": "PENDING",
        "standardApproved": False,
        "negRiskApproved": False,
        "pendingTxHash": "0x" + "a" * 64,
        "networkError": True,
        "raw_tx": "private transaction bytes",
        "private_key": "private key marker",
        **known,
    }
    reads = 0

    def readiness(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        status = "MISSING" if reads == 1 else fresh
        return {
            "ready": False,
            "checks": [],
            "redemptionApproval": {
                "status": status,
                "standardApproved": status == "APPROVED",
                "negRiskApproved": status == "APPROVED",
                "pendingTxHash": "0x" + "b" * 64 if status == "PENDING" else None,
                "networkError": False,
            },
        }

    class Approval:
        def __init__(self, _wallet, _ledger):
            pass

        def start(self):
            return pending

    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    monkeypatch.setattr("pm_nautilus.redemption.PolygonWallet", lambda _settings: object())
    monkeypatch.setattr("pm_nautilus.redemption.RedemptionApprovalService", Approval)
    monkeypatch.setattr("pm_nautilus.app.run_readiness", readiness)
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        csrf = {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}
        assert (
            client.post("/api/live/wallet/import", json=_payload(), headers=csrf).status_code == 200
        )
        response = client.post(
            "/api/live/wallet/approve-redemption",
            json={"confirmation": "APPROVE REDEMPTION"},
            headers=csrf,
        )
        assert response.status_code == 200, response.text
        approval = response.json()["redemptionApproval"]
        assert approval["status"] == expected
        if fresh == "PENDING":
            assert approval["pendingTxHash"] == "0x" + "b" * 64
            assert approval["networkError"] is False
        assert "private transaction bytes" not in response.text
        assert "private key marker" not in response.text
        assert response.json()["enabled"] is False


@pytest.mark.parametrize("account_unknown", [False, True])
def test_preparation_failure_returns_safe_durable_error_without_any_transaction(
    tmp_path, monkeypatch, account_unknown
):
    from test_live_wallet_api import _payload, _settings
    from test_redemption_approval import ControlledWallet, saved
    from web3.exceptions import Web3RPCError

    wallet = ControlledWallet()
    wallet.signer.address = wallet.funder = ADDRESS
    wallet.signature_type = 0

    def fail_prepare(_neg_risk):
        raise Web3RPCError("insufficient funds: private provider marker")

    wallet.prepare_approval = fail_prepare
    reads = 0

    def readiness(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        if reads > 1 and account_unknown:
            raise ConnectionError("controlled account failure")
        return {
            "ready": False,
            "checks": [],
            "redemptionApproval": {
                "status": "MISSING" if reads == 1 else "FAILED",
                "standardApproved": False,
                "negRiskApproved": False,
                "pendingTxHash": None,
                "networkError": False,
            },
        }

    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    monkeypatch.setattr("pm_nautilus.redemption.PolygonWallet", lambda _settings: wallet)
    monkeypatch.setattr("pm_nautilus.app.run_readiness", readiness)
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        csrf = {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}
        assert (
            client.post("/api/live/wallet/import", json=_payload(), headers=csrf).status_code == 200
        )
        response = client.post(
            "/api/live/wallet/approve-redemption",
            json={"confirmation": "APPROVE REDEMPTION"},
            headers=csrf,
        )
        assert response.status_code == 200, response.text
        approval = response.json()["redemptionApproval"]
        assert approval["status"] == ("UNKNOWN" if account_unknown else "FAILED")
        assert approval["errorCode"] == "INSUFFICIENT_POL"
        assert "POL" in approval["errorMessage"] and approval["stage"] == "prepare"
        assert approval["pendingTxHash"] is None
        assert "private provider marker" not in response.text
        assert client.get("/api/live/wallet").json()["redemptionApproval"] == approval
        assert wallet.prepared == [] and wallet.broadcasts == []
        record = saved(tmp_path / "LIVE" / "state.sqlite")
        assert record["status"] == "FAILED" and record["pending"] is None
        assert response.json()["readiness"]["ready"] is False
        assert client.get("/api/health").json()["liveExecutionEnabled"] is False

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("controlled RPC unavailable on reboot")

    def no_wallet(*_args, **_kwargs):
        pytest.fail("Saved preparation diagnostics must not initialize an onchain wallet")

    monkeypatch.setattr("pm_nautilus.app.run_readiness", unavailable)
    monkeypatch.setattr("pm_nautilus.redemption.PolygonWallet", no_wallet)
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        csrf = {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}
        rebooted = client.post(
            "/api/live/wallet/unlock",
            json={"vaultPassword": _payload()["vaultPassword"]},
            headers=csrf,
        )
        assert rebooted.status_code == 200, rebooted.text
        result = rebooted.json()
        diagnostic = result["redemptionApproval"]
        assert diagnostic["status"] == "UNKNOWN" and diagnostic["pendingTxHash"] is None
        assert diagnostic["standardApproved"] is None and diagnostic["negRiskApproved"] is None
        assert diagnostic["errorCode"] == "INSUFFICIENT_POL" and diagnostic["stage"] == "prepare"
        assert diagnostic["errorMessage"] == approval["errorMessage"]
        assert "private provider marker" not in rebooted.text
        assert result["status"] == "BLOCKED" and result["enabled"] is False
        assert result["readiness"]["ready"] is False
        checked = client.post("/api/live/wallet/check", json={}, headers=csrf)
        assert checked.json()["redemptionApproval"] == diagnostic
        assert (
            client.post(
                "/api/live/wallet/enable", json={"confirmation": "ENABLE LIVE"}, headers=csrf
            ).status_code
            == 400
        )
        assert client.get("/api/live/wallet").json()["redemptionApproval"] == diagnostic
        assert saved(tmp_path / "LIVE" / "state.sqlite") == record
        assert wallet.prepared == [] and wallet.broadcasts == []


@pytest.mark.parametrize(
    "invalid",
    [None, "identity", "version", "raw", "nonce", "operator", "remaining", "failed"],
)
def test_reboot_unlock_reads_only_valid_saved_pending_when_rpc_is_unavailable(
    tmp_path, monkeypatch, invalid
):
    from test_live_wallet_api import _payload, _settings
    from test_redemption_approval import ControlledWallet, saved

    wallet = ControlledWallet()
    wallet.signer.address = wallet.funder = ADDRESS
    wallet.signature_type = 0
    wallet.broadcast_fails = True
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    monkeypatch.setattr("pm_nautilus.redemption.PolygonWallet", lambda _settings: wallet)
    monkeypatch.setattr(
        "pm_nautilus.app.run_readiness", lambda *_args, **_kwargs: {"ready": False, "checks": []}
    )
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        csrf = {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}
        assert (
            client.post("/api/live/wallet/import", json=_payload(), headers=csrf).status_code == 200
        )
        response = client.post(
            "/api/live/wallet/approve-redemption",
            json={"confirmation": "APPROVE REDEMPTION"},
            headers=csrf,
        )
        assert response.status_code == 200
        original_hash = response.json()["redemptionApproval"]["pendingTxHash"]
    record = saved(tmp_path / "LIVE" / "state.sqlite")
    record["errorMessage"] = "provider-secret-must-not-appear"
    if invalid == "identity":
        record["identity"]["funder"] = "0x" + "33" * 20
    elif invalid == "version":
        record["version"] = True
    elif invalid == "raw":
        record["pending"]["raw_tx"] = "0x0101"
    elif invalid == "nonce":
        record["pending"]["nonce"] = True
    elif invalid == "operator":
        record["pending"]["operator"] = "0x" + "33" * 20
    elif invalid == "remaining":
        record["remaining"] = [1, True]
    elif invalid == "failed":
        record["status"] = "FAILED"
    with sqlite3.connect(tmp_path / "LIVE" / "state.sqlite") as db:
        db.execute("UPDATE meta SET value=? WHERE key='redemption_approval'", (json.dumps(record),))

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("controlled RPC unavailable")

    def no_wallet(*_args, **_kwargs):
        pytest.fail("Reading saved approval must not initialize an onchain wallet")

    monkeypatch.setattr("pm_nautilus.app.run_readiness", unavailable)
    monkeypatch.setattr("pm_nautilus.redemption.PolygonWallet", no_wallet)
    app = create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        assert client.get("/api/live/wallet").json()["redemptionApproval"] is None
        csrf = {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}
        unlocked = client.post(
            "/api/live/wallet/unlock",
            json={"vaultPassword": _payload()["vaultPassword"]},
            headers=csrf,
        )
        assert unlocked.status_code == 200, unlocked.text
        result = unlocked.json()
        approval = result["redemptionApproval"]
        assert approval["status"] == ("UNKNOWN" if invalid else "PENDING")
        assert approval["pendingTxHash"] == (None if invalid else original_hash)
        if not invalid:
            assert approval["standardApproved"] is False and approval["negRiskApproved"] is False
            assert approval["networkError"] is True
            assert approval["errorCode"] == "RPC_RESULT_UNKNOWN"
        assert "provider-secret-must-not-appear" not in unlocked.text
        assert "raw_tx" not in unlocked.text and KEY not in unlocked.text
        assert result["status"] == "BLOCKED" and result["readiness"]["ready"] is False
        assert result["enabled"] is False and set(app.state.runtimes) == {"TEST"}
        assert client.get("/api/live/wallet").json()["redemptionApproval"] == approval
        assert (
            client.post(
                "/api/live/wallet/enable",
                json={"confirmation": "ENABLE LIVE"},
                headers=csrf,
            ).status_code
            == 400
        )
        assert client.get("/api/live/wallet").json()["redemptionApproval"] == approval
        assert saved(tmp_path / "LIVE" / "state.sqlite") == record
        assert wallet.prepared == [False] and len(wallet.broadcasts) == 1


@pytest.mark.parametrize("failure", ["prepare", "receipt", "version", "stage", "code", "remaining"])
def test_replacement_clears_only_unsigned_prepare_failure_and_keeps_nonce_floor(
    tmp_path, monkeypatch, failure
):
    from test_live_wallet_api import _payload, _settings
    from test_redemption_approval import ControlledWallet, saved
    from web3.exceptions import Web3RPCError

    wallet = ControlledWallet()
    wallet.signer.address = wallet.funder = ADDRESS
    wallet.signature_type = 0

    def fail_prepare(_neg_risk):
        raise Web3RPCError("insufficient funds: controlled preparation failure")

    if failure != "receipt":
        wallet.prepare_approval = fail_prepare
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    monkeypatch.setattr("pm_nautilus.redemption.PolygonWallet", lambda _settings: wallet)
    monkeypatch.setattr(
        "pm_nautilus.app.run_readiness", lambda *_args, **_kwargs: {"ready": False, "checks": []}
    )
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        csrf = {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}
        assert (
            client.post("/api/live/wallet/import", json=_payload(), headers=csrf).status_code == 200
        )
        response = client.post(
            "/api/live/wallet/approve-redemption",
            json={"confirmation": "APPROVE REDEMPTION"},
            headers=csrf,
        )
        assert response.status_code == 200
        if failure == "receipt":
            wallet.receipt_result = {"status": "FAILED", "amount": 0}
            response = client.post(
                "/api/live/wallet/approve-redemption",
                json={"confirmation": "APPROVE REDEMPTION"},
                headers=csrf,
            )
            assert response.status_code == 200
    path = tmp_path / "LIVE" / "state.sqlite"
    record = saved(path)
    assert record["status"] == "FAILED"
    assert (record["pending"] is not None) is (failure == "receipt")
    if failure == "version":
        record["version"] = True
    elif failure == "stage":
        record["stage"] = "receipt"
    elif failure == "code":
        record["errorCode"] = ["INSUFFICIENT_POL"]
    elif failure == "remaining":
        record["remaining"] = [1, True]
    floor_key = f"polygon_nonce_floor:{ADDRESS.lower()}"
    with sqlite3.connect(path) as db:
        db.execute("UPDATE meta SET value=? WHERE key='redemption_approval'", (json.dumps(record),))
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (floor_key, "9"))
        assert db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM native_events").fetchone()[0] == 0
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        csrf = {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}
        removed = client.post(
            "/api/live/wallet/remove",
            json={
                "vaultPassword": _payload()["vaultPassword"],
                "confirmation": "REPLACE EMPTY WALLET",
            },
            headers=csrf,
        )
        assert removed.status_code == (200 if failure == "prepare" else 400), removed.text
        with sqlite3.connect(path) as db:
            assert (
                db.execute("SELECT value FROM meta WHERE key=?", (floor_key,)).fetchone()[0] == "9"
            )
            approval_row = json.loads(
                db.execute("SELECT value FROM meta WHERE key='redemption_approval'").fetchone()[0]
            )
            bound = json.loads(
                db.execute("SELECT value FROM meta WHERE key='live_wallet_identity'").fetchone()[0]
            )
        if failure != "prepare":
            assert approval_row == record and bound["funder"] == ADDRESS
            assert (tmp_path / "wallet" / "live.vault").is_file()
            return
        assert approval_row is None and bound is None
        assert removed.json()["redemptionApproval"] is None
        other_key = "0x" + "2" * 64
        other_address = Account.from_key(other_key).address
        wallet.signer.address = wallet.funder = other_address
        imported = client.post(
            "/api/live/wallet/import",
            json={**_payload(), "secret": other_key, "publicAddress": other_address},
            headers=csrf,
        )
        assert imported.status_code == 200, imported.text
        approved = client.post(
            "/api/live/wallet/approve-redemption",
            json={"confirmation": "APPROVE REDEMPTION"},
            headers=csrf,
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["redemptionApproval"]["errorCode"] == "INSUFFICIENT_POL"
        assert saved(path)["identity"]["funder"] == other_address
        assert wallet.prepared == [] and wallet.broadcasts == []
        assert client.get("/api/health").json()["liveExecutionEnabled"] is False
        with sqlite3.connect(path) as db:
            assert (
                db.execute("SELECT value FROM meta WHERE key=?", (floor_key,)).fetchone()[0] == "9"
            )
