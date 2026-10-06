"""Password-encrypted LIVE wallet and ledger migration bundle.

The bundle contains a consistent SQLite snapshot and a second, independently
encrypted wallet vault. It never contains a mnemonic or plaintext key on disk.
Only a cold, empty LIVE ledger may be restored.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from Crypto.Cipher import AES
from eth_account import Account
from web3 import Web3

from .wallet_vault import VaultError, _read_vault_bytes, read_vault, write_vault


MAGIC = b"PM-NAUTILUS-LIVE-BACKUP-1\n"
AAD = b"PM-NAUTILUS-LIVE-BACKUP-1"
MAX_BUNDLE = 128 * 1024 * 1024
MAX_DATABASE = 96 * 1024 * 1024


class BackupError(ValueError):
    """A safe public backup failure, never including wallet or SQLite content."""


def _identity(settings):
    return {
        "signer": Account.from_key(settings["private_key"]).address,
        "funder": Web3.to_checksum_address(settings["funder"]),
        "signature_type": settings["signature_type"],
    }


def _same_identity(left, right):
    return (
        isinstance(left, dict)
        and isinstance(right, dict)
        and isinstance(left.get("signer"), str)
        and isinstance(left.get("funder"), str)
        and isinstance(right.get("signer"), str)
        and isinstance(right.get("funder"), str)
        and left["signer"].lower() == right.get("signer", "").lower()
        and left["funder"].lower() == right.get("funder", "").lower()
        and left.get("signature_type") == right.get("signature_type")
    )


def _read_meta(db, key):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _validate_snapshot(db, identity):
    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise BackupError("LIVE 账本完整性检查失败")
    if _read_meta(db, "mode") != "LIVE":
        raise BackupError("备份不是 LIVE 账本")
    if not _same_identity(_read_meta(db, "live_wallet_identity"), identity):
        raise BackupError("备份钱包与 LIVE 账本不一致")


def _snapshot(store):
    if store.mode != "LIVE":
        raise BackupError("只允许备份 LIVE 账本")
    # SQLite's in-memory backup preserves the source WAL header (2/2), which
    # cannot be deserialized without the original WAL. Normalize in an owner-
    # only temporary directory before serializing one self-contained database.
    with tempfile.TemporaryDirectory(prefix=".pm-live-snapshot-", dir=store.path.parent) as scratch:
        with sqlite3.connect(Path(scratch) / "snapshot.sqlite") as snapshot:
            store.db.backup(snapshot)
            snapshot.execute("PRAGMA journal_mode=DELETE")
            raw = snapshot.serialize()
    if not raw or len(raw) > MAX_DATABASE:
        raise BackupError("LIVE 账本超出网页备份大小限制")
    return raw


def _seal(payload, password):
    if not isinstance(password, str) or not 12 <= len(password) <= 4096:
        raise BackupError("备份密码至少需要 12 个字符")
    salt, nonce = os.urandom(16), os.urandom(12)
    key = hashlib.scrypt(
        password.encode(), salt=salt, n=1 << 16, r=8, p=1, dklen=32, maxmem=128 * 1024 * 1024
    )
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)
    cipher.update(AAD)
    encrypted, tag = cipher.encrypt_and_digest(payload)
    bundle = MAGIC + salt + nonce + encrypted + tag
    if len(bundle) > MAX_BUNDLE:
        raise BackupError("LIVE 备份超出网页下载大小限制")
    return bundle


def _unseal(bundle, password):
    minimum = len(MAGIC) + 16 + 12 + 4 + 16
    if (
        not isinstance(bundle, bytes)
        or not minimum <= len(bundle) <= MAX_BUNDLE
        or not bundle.startswith(MAGIC)
    ):
        raise BackupError("LIVE 备份文件格式或大小无效")
    if not isinstance(password, str) or not 12 <= len(password) <= 4096:
        raise BackupError("请填写备份密码")
    offset = len(MAGIC)
    salt, nonce = bundle[offset : offset + 16], bundle[offset + 16 : offset + 28]
    ciphertext, tag = bundle[offset + 28 : -16], bundle[-16:]
    try:
        key = hashlib.scrypt(
            password.encode(), salt=salt, n=1 << 16, r=8, p=1, dklen=32, maxmem=128 * 1024 * 1024
        )
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)
        cipher.update(AAD)
        return cipher.decrypt_and_verify(ciphertext, tag)
    except Exception:
        raise BackupError("备份解密失败；请检查密码或文件") from None


def _empty(store):
    if store.mode != "LIVE" or store.get("live_wallet_identity") is not None:
        return False
    for table in ("native_events", "intents", "tokens", "books"):
        if store.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            return False
    business = store.get("business")
    return not any(business.get(key) for key in ("cycles", "targets", "claims", "settled"))


def export_live_bundle(store, vault_path: Path, password: str) -> bytes:
    """Export a self-contained encrypted wallet and consistent LIVE DB snapshot."""
    try:
        settings = read_vault(vault_path, password)
        identity = _identity(settings)
        vault = _read_vault_bytes(vault_path)
        database = _snapshot(store)
        with sqlite3.connect(":memory:") as snapshot:
            snapshot.deserialize(database)
            _validate_snapshot(snapshot, identity)
        payload = len(vault).to_bytes(4, "big") + vault + database
        return _seal(payload, password)
    except BackupError:
        raise
    except (VaultError, ValueError):
        raise BackupError("钱包密码或 LIVE 账本不匹配，无法备份") from None
    except Exception:
        raise BackupError("LIVE 备份未完成，请稍后重试") from None


def restore_live_bundle(bundle: bytes, password: str, store, vault_path: Path) -> dict:
    """Restore only into an empty, cold LIVE store; never overwrite trade history."""
    if not _empty(store):
        raise BackupError("目标 LIVE 账本已有内容，不能覆盖")
    payload = _unseal(bundle, password)
    vault_size = int.from_bytes(payload[:4], "big")
    if not 0 < vault_size <= 64 * 1024 or len(payload) <= 4 + vault_size:
        raise BackupError("LIVE 备份内容无效")
    vault_bytes, database = payload[4 : 4 + vault_size], payload[4 + vault_size :]
    if len(database) > MAX_DATABASE:
        raise BackupError("LIVE 备份账本过大")
    try:
        with tempfile.TemporaryDirectory(prefix="pm-live-restore-") as scratch:
            temporary_vault = Path(scratch) / "live.vault"
            fd = os.open(temporary_vault, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(vault_bytes)
            settings = read_vault(temporary_vault, password)
        identity = _identity(settings)
        with sqlite3.connect(":memory:") as snapshot:
            snapshot.deserialize(database)
            _validate_snapshot(snapshot, identity)
            # Capture the untouched destination before creating its vault. If
            # this snapshot fails, the empty installation stays unconfigured.
            empty_database = _snapshot(store)
            if vault_path.exists() or vault_path.is_symlink():
                if read_vault(vault_path, password) != settings:
                    raise BackupError("目标服务器已有其他钱包，不能覆盖")
                created_vault = False
            else:
                write_vault(vault_path, settings, password)
                created_vault = True
            try:
                snapshot.backup(store.db)
                store.db.execute("PRAGMA journal_mode=WAL")
            except Exception:
                with sqlite3.connect(":memory:") as original:
                    original.deserialize(empty_database)
                    original.backup(store.db)
                if created_vault:
                    vault_path.unlink()
                raise BackupError("LIVE 账本恢复失败，目标已回滚") from None
    except BackupError:
        raise
    except Exception:
        raise BackupError("LIVE 备份校验或恢复失败，目标未启用") from None
    return identity
