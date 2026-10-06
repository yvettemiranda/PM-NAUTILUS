import pytest
from eth_account import Account

import pm_nautilus.live_backup as live_backup
from pm_nautilus.live_backup import BackupError, export_live_bundle, restore_live_bundle
from pm_nautilus.store import Store
from pm_nautilus.wallet_vault import write_vault


KEY = "0x" + "1" * 64
ADDRESS = Account.from_key(KEY).address
PASSWORD = "migration-password-123"


def _settings():
    return {
        "private_key": KEY,
        "api_key": "api-key",
        "api_secret": "api-secret",
        "passphrase": "api-passphrase",
        "funder": ADDRESS,
        "rpc_url": "https://polygon.example",
        "signature_type": 0,
        "auto_approve_redemption": False,
    }


def test_encrypted_live_backup_restores_wallet_and_ledger(tmp_path):
    source = Store(tmp_path / "source" / "LIVE" / "state.sqlite", "LIVE")
    original_vault = tmp_path / "source" / "wallet" / "live.vault"
    identity = write_vault(original_vault, _settings(), PASSWORD)
    source.put("live_wallet_identity", identity)
    source.save_intent(
        "owned", {"event_id": "event", "token_id": "1", "side": "BUY", "terminal": True}
    )
    bundle = export_live_bundle(source, original_vault, PASSWORD)
    assert KEY.encode() not in bundle
    assert b"owned" not in bundle

    destination = Store(tmp_path / "dest" / "LIVE" / "state.sqlite", "LIVE")
    restored_vault = tmp_path / "dest" / "wallet" / "live.vault"
    assert restore_live_bundle(bundle, PASSWORD, destination, restored_vault) == identity
    assert destination.get("live_wallet_identity") == identity
    assert destination.intent("owned")["token_id"] == "1"
    with pytest.raises(BackupError, match="已有内容"):
        restore_live_bundle(bundle, PASSWORD, destination, restored_vault)
    source.close()
    destination.close()


def test_backup_rejects_wrong_password_and_tamper_without_writing(tmp_path):
    source = Store(tmp_path / "source" / "LIVE" / "state.sqlite", "LIVE")
    vault = tmp_path / "source" / "wallet" / "live.vault"
    source.put("live_wallet_identity", write_vault(vault, _settings(), PASSWORD))
    bundle = export_live_bundle(source, vault, PASSWORD)
    destination = Store(tmp_path / "dest" / "LIVE" / "state.sqlite", "LIVE")
    target = tmp_path / "dest" / "wallet" / "live.vault"
    with pytest.raises(BackupError, match="解密"):
        restore_live_bundle(bundle, "incorrect-password-123", destination, target)
    altered = bundle[:-17] + bytes([bundle[-17] ^ 1]) + bundle[-16:]
    with pytest.raises(BackupError, match="解密"):
        restore_live_bundle(altered, PASSWORD, destination, target)
    assert not target.exists()
    assert destination.get("live_wallet_identity") is None
    source.close()
    destination.close()


def test_backup_cannot_restore_over_existing_live_history(tmp_path):
    source = Store(tmp_path / "source" / "LIVE" / "state.sqlite", "LIVE")
    vault = tmp_path / "source" / "wallet" / "live.vault"
    source.put("live_wallet_identity", write_vault(vault, _settings(), PASSWORD))
    bundle = export_live_bundle(source, vault, PASSWORD)
    destination = Store(tmp_path / "dest" / "LIVE" / "state.sqlite", "LIVE")
    destination.save_intent(
        "already", {"event_id": "e", "token_id": "1", "side": "BUY", "terminal": True}
    )
    with pytest.raises(BackupError, match="已有内容"):
        restore_live_bundle(
            bundle, PASSWORD, destination, tmp_path / "dest" / "wallet" / "live.vault"
        )
    source.close()
    destination.close()


def test_restore_snapshot_failure_leaves_new_server_unconfigured(tmp_path, monkeypatch):
    source = Store(tmp_path / "source" / "LIVE" / "state.sqlite", "LIVE")
    vault = tmp_path / "source" / "wallet" / "live.vault"
    source.put("live_wallet_identity", write_vault(vault, _settings(), PASSWORD))
    bundle = export_live_bundle(source, vault, PASSWORD)
    destination = Store(tmp_path / "dest" / "LIVE" / "state.sqlite", "LIVE")
    target = tmp_path / "dest" / "wallet" / "live.vault"

    def unavailable(_store):
        raise OSError("snapshot unavailable")

    monkeypatch.setattr(live_backup, "_snapshot", unavailable)
    with pytest.raises(BackupError, match="未启用"):
        restore_live_bundle(bundle, PASSWORD, destination, target)
    assert not target.exists()
    assert destination.get("live_wallet_identity") is None
    source.close()
    destination.close()
