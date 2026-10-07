"""Password-encrypted LIVE wallet and ledger migration bundle.

Version 2 compresses and encrypts a consistent SQLite snapshot in bounded
chunks. All temporary files are placed in an owner-only directory on the LIVE
data volume, never in the container's small /tmp tmpfs. A restore authenticates
and validates the entire bundle before it changes the destination ledger.
"""

import hashlib
import fcntl
import json
import os
import shutil
import sqlite3
import tempfile
import zlib
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from Crypto.Cipher import AES
from eth_account import Account
from web3 import Web3

from .wallet_vault import VaultError, _read_vault_bytes, read_vault, write_vault


MAGIC_V1 = b"PM-NAUTILUS-LIVE-BACKUP-1\n"
MAGIC_V2 = b"PM-NAUTILUS-LIVE-BACKUP-2\n"
MAX_BUNDLE_V1 = 128 * 1024 * 1024
MAX_BUNDLE = 2 * 1024 * 1024 * 1024
MAX_DATABASE = 4 * 1024 * 1024 * 1024
CHUNK = 1024 * 1024


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


def _new_private_file(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    return path


def _snapshot_file(store, scratch):
    """Make a self-contained committed snapshot without crossing thread affinity.

    A separate read-only connection is opened here so callers may run a large
    export in a worker thread while the trading connection stays on its owner
    thread. SQLite's backup API gives a consistent database image under WAL.
    """
    if store.mode != "LIVE":
        raise BackupError("只允许备份 LIVE 账本")
    snapshot_path = _new_private_file(scratch / "snapshot.sqlite")
    source_uri = f"{store.path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True)) as source:
        with closing(sqlite3.connect(snapshot_path)) as snapshot:
            source.backup(snapshot)
            snapshot.execute("PRAGMA journal_mode=DELETE")
    size = snapshot_path.stat().st_size
    if not 0 < size <= MAX_DATABASE:
        raise BackupError("LIVE 账本超出网页备份大小限制")
    return snapshot_path


def _check_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 4096:
        raise BackupError("备份密码至少需要 12 个字符")


def _key(password, salt):
    return hashlib.scrypt(
        password.encode(), salt=salt, n=1 << 16, r=8, p=1, dklen=32, maxmem=128 * 1024 * 1024
    )


def _encrypt_file(snapshot_path, vault, password, output_path):
    _check_password(password)
    if not 0 < len(vault) <= 64 * 1024:
        raise BackupError("加密钱包文件大小无效")
    salt, nonce = os.urandom(16), os.urandom(12)
    cipher = AES.new(_key(password, salt), AES.MODE_GCM, nonce=nonce, mac_len=16)
    cipher.update(MAGIC_V2)
    size = snapshot_path.stat().st_size
    prefix = len(vault).to_bytes(4, "big") + vault + size.to_bytes(8, "big")
    compressor = zlib.compressobj(level=6)
    with snapshot_path.open("rb") as source, output_path.open("wb") as output:
        output.write(MAGIC_V2 + salt + nonce)
        output.write(cipher.encrypt(prefix))
        while chunk := source.read(CHUNK):
            compressed = compressor.compress(chunk)
            if compressed:
                output.write(cipher.encrypt(compressed))
            if output.tell() > MAX_BUNDLE:
                raise BackupError("LIVE 备份超出网页下载大小限制")
        output.write(cipher.encrypt(compressor.flush()))
        output.write(cipher.digest())
        if output.tell() > MAX_BUNDLE:
            raise BackupError("LIVE 备份超出网页下载大小限制")


def _empty(store):
    if store.mode != "LIVE":
        return False
    with closing(sqlite3.connect(store.path)) as db:
        if _read_meta(db, "mode") != "LIVE" or _read_meta(db, "live_wallet_identity") is not None:
            return False
        for table in ("native_events", "intents", "tokens", "books"):
            if db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return False
        business = _read_meta(db, "business")
        return isinstance(business, dict) and not any(
            business.get(key) for key in ("cycles", "targets", "claims", "settled")
        )


def cleanup_live_bundle_file(path):
    """Remove a completed or abandoned export, including its snapshot directory."""
    path = Path(path)
    if path.name != "bundle.pmnb" or not path.parent.name.startswith(".pm-live-export-"):
        raise ValueError("无效的临时备份路径")
    shutil.rmtree(path.parent)


def export_live_bundle_file(store, vault_path: Path, password: str) -> Path:
    """Create an encrypted .pmnb in /data using bounded memory; caller cleans up."""
    scratch = Path(tempfile.mkdtemp(prefix=".pm-live-export-", dir=store.path.parent))
    try:
        settings = read_vault(vault_path, password)
        identity = _identity(settings)
        vault = _read_vault_bytes(vault_path)
        snapshot_path = _snapshot_file(store, scratch)
        with sqlite3.connect(snapshot_path) as snapshot:
            _validate_snapshot(snapshot, identity)
        output_path = _new_private_file(scratch / "bundle.pmnb")
        _encrypt_file(snapshot_path, vault, password, output_path)
        snapshot_path.unlink()
        return output_path
    except BackupError:
        shutil.rmtree(scratch)
        raise
    except (VaultError, ValueError):
        shutil.rmtree(scratch)
        raise BackupError("钱包密码或 LIVE 账本不匹配，无法备份") from None
    except Exception:
        shutil.rmtree(scratch)
        raise BackupError("LIVE 备份未完成，请稍后重试") from None


def export_final_live_bundle_file(data_dir: Path, password: str, destination: Path) -> str:
    """Export the final LIVE state while holding the application's process lock.

    The caller must leave the old application stopped after this returns. The
    exclusive lock prevents a concurrent application start during the export.
    """
    root = Path(data_dir)
    destination = Path(destination)
    database = root / "LIVE" / "state.sqlite"
    vault = root / "wallet" / "live.vault"
    if not database.is_file() or not vault.is_file():
        raise BackupError("LIVE 账本或加密钱包不存在")
    if not destination.parent.is_dir() or destination.exists() or destination.is_symlink():
        raise BackupError("最终备份目标目录不存在或文件已存在")
    bundle_path = None
    staged_path = None
    with (root / "process.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("旧应用仍在运行；须停机后再取得最终备份") from None
        try:
            source = SimpleNamespace(mode="LIVE", path=database)
            bundle_path = export_live_bundle_file(source, vault, password)
            with tempfile.NamedTemporaryFile(
                prefix=".pm-final-", dir=destination.parent, delete=False
            ) as staged:
                staged_path = Path(staged.name)
                digest = hashlib.sha256()
                with bundle_path.open("rb") as source_file:
                    while chunk := source_file.read(CHUNK):
                        staged.write(chunk)
                        digest.update(chunk)
                staged.flush()
                os.fsync(staged.fileno())
            os.link(staged_path, destination)
            return digest.hexdigest()
        except FileExistsError:
            raise BackupError("最终备份文件已存在，不能覆盖") from None
        finally:
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)
            if bundle_path is not None:
                cleanup_live_bundle_file(bundle_path)
            fcntl.flock(lock, fcntl.LOCK_UN)


def _decrypt_v2(bundle_path, password, scratch):
    _check_password(password)
    size = bundle_path.stat().st_size
    minimum = len(MAGIC_V2) + 16 + 12 + 4 + 8 + 16
    if not minimum <= size <= MAX_BUNDLE:
        raise BackupError("LIVE 备份文件格式或大小无效")
    payload_path = _new_private_file(scratch / "payload.bin")
    with bundle_path.open("rb") as bundle, payload_path.open("wb") as payload:
        header = bundle.read(len(MAGIC_V2) + 28)
        if not header.startswith(MAGIC_V2) or len(header) != len(MAGIC_V2) + 28:
            raise BackupError("LIVE 备份文件格式或大小无效")
        salt, nonce = header[len(MAGIC_V2) : -12], header[-12:]
        cipher = AES.new(_key(password, salt), AES.MODE_GCM, nonce=nonce, mac_len=16)
        cipher.update(MAGIC_V2)
        ciphertext_remaining = size - len(header) - 16
        while ciphertext_remaining:
            chunk = bundle.read(min(CHUNK, ciphertext_remaining))
            if not chunk:
                raise BackupError("LIVE 备份文件格式或大小无效")
            payload.write(cipher.decrypt(chunk))
            ciphertext_remaining -= len(chunk)
        try:
            cipher.verify(bundle.read(16))
        except ValueError:
            raise BackupError("备份解密失败；请检查密码或文件") from None
    return payload_path


def _unpack_v2(payload_path, scratch):
    snapshot_path = _new_private_file(scratch / "restored.sqlite")
    with payload_path.open("rb") as payload, snapshot_path.open("wb") as snapshot:
        head = payload.read(4)
        if len(head) != 4:
            raise BackupError("LIVE 备份内容无效")
        vault_size = int.from_bytes(head, "big")
        if not 0 < vault_size <= 64 * 1024:
            raise BackupError("LIVE 备份内容无效")
        vault = payload.read(vault_size)
        declared_size_raw = payload.read(8)
        if len(vault) != vault_size or len(declared_size_raw) != 8:
            raise BackupError("LIVE 备份内容无效")
        declared_size = int.from_bytes(declared_size_raw, "big")
        if not 0 < declared_size <= MAX_DATABASE:
            raise BackupError("LIVE 备份账本过大")
        decompressor = zlib.decompressobj()
        written = 0
        while chunk := payload.read(CHUNK):
            while chunk:
                plain = decompressor.decompress(chunk, CHUNK)
                written += len(plain)
                if written > declared_size:
                    raise BackupError("LIVE 备份账本大小无效")
                snapshot.write(plain)
                chunk = decompressor.unconsumed_tail
                if decompressor.eof:
                    if chunk or decompressor.unused_data or payload.read(1):
                        raise BackupError("LIVE 备份内容无效")
                    break
        if not decompressor.eof or written != declared_size:
            raise BackupError("LIVE 备份账本大小无效")
    return vault, snapshot_path


def _unpack_v1(bundle_path, password, scratch):
    """Read small backups produced before streaming was introduced."""
    _check_password(password)
    if bundle_path.stat().st_size > MAX_BUNDLE_V1:
        raise BackupError("LIVE 备份文件格式或大小无效")
    bundle = bundle_path.read_bytes()
    minimum = len(MAGIC_V1) + 16 + 12 + 4 + 16
    if not minimum <= len(bundle) <= MAX_BUNDLE_V1 or not bundle.startswith(MAGIC_V1):
        raise BackupError("LIVE 备份文件格式或大小无效")
    offset = len(MAGIC_V1)
    salt, nonce = bundle[offset : offset + 16], bundle[offset + 16 : offset + 28]
    try:
        cipher = AES.new(_key(password, salt), AES.MODE_GCM, nonce=nonce, mac_len=16)
        cipher.update(b"PM-NAUTILUS-LIVE-BACKUP-1")
        payload = cipher.decrypt_and_verify(bundle[offset + 28 : -16], bundle[-16:])
    except Exception:
        raise BackupError("备份解密失败；请检查密码或文件") from None
    vault_size = int.from_bytes(payload[:4], "big")
    if not 0 < vault_size <= 64 * 1024 or len(payload) <= 4 + vault_size:
        raise BackupError("LIVE 备份内容无效")
    vault = payload[4 : 4 + vault_size]
    database = payload[4 + vault_size :]
    if len(database) > MAX_DATABASE:
        raise BackupError("LIVE 备份账本过大")
    snapshot_path = _new_private_file(scratch / "restored.sqlite")
    snapshot_path.write_bytes(database)
    return vault, snapshot_path


def _restore_snapshot(snapshot_path, vault_bytes, password, store, vault_path):
    temporary_vault = _new_private_file(snapshot_path.parent / "live.vault")
    temporary_vault.write_bytes(vault_bytes)
    settings = read_vault(temporary_vault, password)
    identity = _identity(settings)
    with closing(sqlite3.connect(snapshot_path)) as snapshot:
        _validate_snapshot(snapshot, identity)
        # Capture the untouched destination before creating its vault. If this
        # fails, the empty installation stays unconfigured.
        empty_database = _snapshot_file(store, snapshot_path.parent)
        if not _empty(store):
            raise BackupError("目标 LIVE 账本已有内容，不能覆盖")
        if vault_path.exists() or vault_path.is_symlink():
            if read_vault(vault_path, password) != settings:
                raise BackupError("目标服务器已有其他钱包，不能覆盖")
            created_vault = False
        else:
            write_vault(vault_path, settings, password)
            created_vault = True
        with closing(sqlite3.connect(store.path, isolation_level=None)) as destination:
            try:
                snapshot.backup(destination)
                destination.execute("PRAGMA journal_mode=WAL")
            except Exception:
                try:
                    with closing(sqlite3.connect(empty_database)) as original:
                        original.backup(destination)
                    if created_vault:
                        vault_path.unlink()
                except Exception:
                    raise BackupError("LIVE 账本恢复异常，请停止操作并检查服务器备份") from None
                raise BackupError("LIVE 账本恢复失败，目标已回滚") from None
    return identity


def restore_live_bundle_file(bundle_path: Path, password: str, store, vault_path: Path) -> dict:
    """Restore a streamed upload into a cold empty LIVE store."""
    if not _empty(store):
        raise BackupError("目标 LIVE 账本已有内容，不能覆盖")
    bundle_path = Path(bundle_path)
    try:
        if not bundle_path.is_file() or not 0 < bundle_path.stat().st_size <= MAX_BUNDLE:
            raise BackupError("LIVE 备份文件格式或大小无效")
        with tempfile.TemporaryDirectory(
            prefix=".pm-live-restore-", dir=store.path.parent
        ) as folder:
            scratch = Path(folder)
            with bundle_path.open("rb") as bundle:
                magic = bundle.read(len(MAGIC_V2))
            if magic == MAGIC_V2:
                payload_path = _decrypt_v2(bundle_path, password, scratch)
                vault_bytes, snapshot_path = _unpack_v2(payload_path, scratch)
                payload_path.unlink()
            elif magic == MAGIC_V1:
                vault_bytes, snapshot_path = _unpack_v1(bundle_path, password, scratch)
            else:
                raise BackupError("LIVE 备份文件格式或大小无效")
            return _restore_snapshot(snapshot_path, vault_bytes, password, store, vault_path)
    except BackupError:
        raise
    except Exception:
        raise BackupError("LIVE 备份校验或恢复失败，目标未启用") from None


def export_live_bundle(store, vault_path: Path, password: str) -> bytes:
    """Compatibility helper for small in-process callers; use the file API on web."""
    output_path = export_live_bundle_file(store, vault_path, password)
    try:
        return output_path.read_bytes()
    finally:
        cleanup_live_bundle_file(output_path)


def restore_live_bundle(bundle: bytes, password: str, store, vault_path: Path) -> dict:
    """Compatibility helper for existing in-process callers; use file API on web."""
    if not isinstance(bundle, bytes) or not 0 < len(bundle) <= MAX_BUNDLE:
        raise BackupError("LIVE 备份文件格式或大小无效")
    with tempfile.TemporaryDirectory(prefix=".pm-live-upload-", dir=store.path.parent) as folder:
        bundle_path = _new_private_file(Path(folder) / "upload.pmnb")
        bundle_path.write_bytes(bundle)
        return restore_live_bundle_file(bundle_path, password, store, vault_path)
