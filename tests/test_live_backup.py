import os
import fcntl
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from Crypto.Cipher import AES

import pytest
from eth_account import Account

import pm_nautilus.live_backup as live_backup
from pm_nautilus.live_backup import (
    BackupError,
    cleanup_live_bundle_file,
    export_live_bundle,
    export_live_bundle_file,
    export_final_live_bundle_file,
    restore_live_bundle,
    restore_live_bundle_file,
)
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

    def unavailable(_store, _scratch):
        raise OSError("snapshot unavailable")

    monkeypatch.setattr(live_backup, "_snapshot_file", unavailable)
    with pytest.raises(BackupError, match="未启用"):
        restore_live_bundle(bundle, PASSWORD, destination, target)
    assert not target.exists()
    assert destination.get("live_wallet_identity") is None
    source.close()
    destination.close()


def test_streamed_backup_handles_database_larger_than_old_limit(tmp_path):
    source = Store(tmp_path / "source" / "LIVE" / "state.sqlite", "LIVE")
    original_vault = tmp_path / "source" / "wallet" / "live.vault"
    identity = write_vault(original_vault, _settings(), PASSWORD)
    source.put("live_wallet_identity", identity)
    # SQLite creates the large blob without a matching Python allocation.
    source.db.execute("CREATE TABLE bulky(payload BLOB NOT NULL)")
    source.db.execute("INSERT INTO bulky VALUES(zeroblob(100 * 1024 * 1024))")
    with ThreadPoolExecutor(max_workers=1) as pool:
        bundle_path = pool.submit(
            export_live_bundle_file, source, original_vault, PASSWORD
        ).result()
    assert bundle_path.stat().st_size < 2 * 1024 * 1024
    assert os.stat(bundle_path).st_mode & 0o777 == 0o600
    assert os.stat(bundle_path.parent).st_mode & 0o777 == 0o700
    assert not (bundle_path.parent / "snapshot.sqlite").exists()

    destination = Store(tmp_path / "dest" / "LIVE" / "state.sqlite", "LIVE")
    restored_vault = tmp_path / "dest" / "wallet" / "live.vault"
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert (
            pool.submit(
                restore_live_bundle_file, bundle_path, PASSWORD, destination, restored_vault
            ).result()
            == identity
        )
    assert (
        destination.db.execute("SELECT length(payload) FROM bulky").fetchone()[0]
        == 100 * 1024 * 1024
    )
    cleanup_live_bundle_file(bundle_path)
    assert not bundle_path.parent.exists()
    source.close()
    destination.close()


def test_legacy_bundle_can_still_be_restored(tmp_path):
    source = Store(tmp_path / "source" / "LIVE" / "state.sqlite", "LIVE")
    vault = tmp_path / "source" / "wallet" / "live.vault"
    identity = write_vault(vault, _settings(), PASSWORD)
    source.put("live_wallet_identity", identity)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    snapshot = live_backup._snapshot_file(source, scratch)
    wallet_bytes = vault.read_bytes()
    raw = snapshot.read_bytes()
    salt, nonce = os.urandom(16), os.urandom(12)
    cipher = AES.new(live_backup._key(PASSWORD, salt), AES.MODE_GCM, nonce=nonce)
    cipher.update(b"PM-NAUTILUS-LIVE-BACKUP-1")
    ciphertext, tag = cipher.encrypt_and_digest(
        len(wallet_bytes).to_bytes(4, "big") + wallet_bytes + raw
    )
    bundle = live_backup.MAGIC_V1 + salt + nonce + ciphertext + tag
    path = Path(tmp_path / "old.pmnb")
    path.write_bytes(bundle)

    destination = Store(tmp_path / "dest" / "LIVE" / "state.sqlite", "LIVE")
    target = tmp_path / "dest" / "wallet" / "live.vault"
    assert restore_live_bundle_file(path, PASSWORD, destination, target) == identity
    source.close()
    destination.close()


def test_failed_large_export_removes_private_scratch(tmp_path, monkeypatch):
    source = Store(tmp_path / "source" / "LIVE" / "state.sqlite", "LIVE")
    vault = tmp_path / "source" / "wallet" / "live.vault"
    source.put("live_wallet_identity", write_vault(vault, _settings(), PASSWORD))
    monkeypatch.setattr(live_backup, "MAX_BUNDLE", 40)
    with pytest.raises(BackupError, match="超出网页下载大小限制"):
        export_live_bundle_file(source, vault, PASSWORD)
    assert list(source.path.parent.glob(".pm-live-export-*")) == []
    source.close()


def test_final_export_requires_stopped_instance_and_restores_latest_ledger(tmp_path):
    root = tmp_path / "old"
    source = Store(root / "LIVE" / "state.sqlite", "LIVE")
    vault = root / "wallet" / "live.vault"
    identity = write_vault(vault, _settings(), PASSWORD)
    source.put("live_wallet_identity", identity)
    source.save_intent(
        "latest", {"event_id": "event", "token_id": "1", "side": "BUY", "terminal": True}
    )
    source.close()
    output_dir = tmp_path / "backups"
    output_dir.mkdir()
    output = output_dir / "final.pmnb"

    lock_path = root / "process.lock"
    with lock_path.open("a+b") as running_lock:
        fcntl.flock(running_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BackupError, match="仍在运行"):
            export_final_live_bundle_file(root, PASSWORD, output)
        assert not output.exists()

    digest = export_final_live_bundle_file(root, PASSWORD, output)
    assert digest == hashlib.sha256(output.read_bytes()).hexdigest()
    assert os.stat(output).st_mode & 0o777 == 0o600
    with pytest.raises(BackupError, match="已存在"):
        export_final_live_bundle_file(root, PASSWORD, output)

    destination = Store(tmp_path / "new" / "LIVE" / "state.sqlite", "LIVE")
    assert (
        restore_live_bundle_file(
            output, PASSWORD, destination, tmp_path / "new" / "wallet" / "live.vault"
        )
        == identity
    )
    assert destination.intent("latest")["token_id"] == "1"
    destination.close()
