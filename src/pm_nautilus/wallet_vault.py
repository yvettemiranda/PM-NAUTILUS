"""Offline wallet import and encrypted LIVE credential storage.

The caller must keep the mnemonic out of URLs, logs, settings, and responses.
This module never calls a network service or signs an order. A vault contains
only the derived signer key and the existing LIVE credential fields.
"""

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from bip_utils import Bip39MnemonicValidator, Bip39SeedGenerator, Bip44, Bip44Changes, Bip44Coins
from Crypto.Cipher import AES
from eth_account import Account
from eth_utils import to_checksum_address


_FORMAT = "pm-nautilus-live-vault"
_VERSION = 1
_KDF_N = 1 << 16
_KDF_R = 8
_KDF_P = 1
_KDF_MAXMEM = 128 * 1024 * 1024
_MAX_FILE = 64 * 1024
_SETTINGS_KEYS = frozenset(
    {
        "private_key",
        "api_key",
        "api_secret",
        "passphrase",
        "funder",
        "rpc_url",
        "signature_type",
        "auto_approve_redemption",
    }
)
_METADATA_KEYS = frozenset({"signer", "funder", "signature_type"})


class VaultError(ValueError):
    """A wallet input or encrypted vault is invalid; messages never include secrets."""


class VaultAuthError(VaultError):
    """The unlock password is wrong or the vault authentication failed."""


@dataclass(frozen=True)
class SignerMaterial:
    address: str
    private_key: str = field(repr=False)


def derive_signer(
    kind: str,
    secret: str,
    *,
    mnemonic_passphrase: str = "",
    account_index: int = 0,
) -> SignerMaterial:
    """Import an EVM signer from a private key or 12 BIP39 words.

    For mnemonic imports, ``account_index`` selects
    ``m/44'/60'/0'/0/<account_index>``. The mnemonic and its optional BIP39
    passphrase are never returned or stored; the caller must clear its request
    object after use. The derived address must still be checked against the
    user's actual Polymarket signer before LIVE can start.
    """
    if kind not in {"private_key", "mnemonic"} or not isinstance(secret, str):
        raise VaultError("钱包输入方式无效")
    if kind == "private_key":
        if mnemonic_passphrase or account_index != 0:
            raise VaultError("私钥方式不使用助记词密码或账户序号")
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", secret):
            raise VaultError("私钥格式无效")
        try:
            account = Account.from_key(secret)
        except (TypeError, ValueError):
            raise VaultError("私钥格式无效") from None
        return SignerMaterial(account.address, "0x" + account.key.hex())

    if not isinstance(mnemonic_passphrase, str):
        raise VaultError("助记词附加密码格式无效")
    if len(secret) > 512 or len(mnemonic_passphrase) > 1024:
        raise VaultError("助记词或附加密码过长")
    if type(account_index) is not int or not 0 <= account_index < 2**31:
        raise VaultError("钱包账户序号无效")
    words = secret.split()
    if len(words) != 12:
        raise VaultError("请填写完整的 12 个助记词")
    mnemonic = " ".join(words)
    try:
        if not Bip39MnemonicValidator().IsValid(mnemonic):
            raise VaultError("助记词校验失败；请检查拼写和顺序")
        seed = Bip39SeedGenerator(mnemonic).Generate(mnemonic_passphrase)
        child = (
            Bip44.FromSeed(seed, Bip44Coins.ETHEREUM)
            .Purpose()
            .Coin()
            .Account(0)
            .Change(Bip44Changes.CHAIN_EXT)
            .AddressIndex(account_index)
        )
        private_key = "0x" + child.PrivateKey().Raw().ToHex()
        account = Account.from_key(private_key)
    except VaultError:
        raise
    except Exception:
        # Mnemonic libraries sometimes include the input words in exceptions.
        raise VaultError("助记词无法导入；请检查钱包类型和账户序号") from None
    return SignerMaterial(account.address, private_key)


def _validate_settings(settings: dict) -> dict:
    if not isinstance(settings, dict) or set(settings) != _SETTINGS_KEYS:
        raise VaultError("LIVE 凭据字段不完整或含不支持的字段")
    key = settings["private_key"]
    if not isinstance(key, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", key):
        raise VaultError("LIVE 签名密钥格式无效")
    try:
        signer = Account.from_key(key).address
        funder = to_checksum_address(settings["funder"])
    except (TypeError, ValueError):
        raise VaultError("LIVE 钱包地址或签名密钥无效") from None
    signature_type = settings["signature_type"]
    if type(signature_type) is not int or signature_type not in (0, 2):
        raise VaultError("当前只支持普通钱包或单签 Safe")
    if signature_type == 0 and signer != funder:
        raise VaultError("普通钱包的签名地址与资金地址必须一致")
    for key in ("api_key", "api_secret", "passphrase"):
        if not isinstance(settings[key], str) or not settings[key].strip():
            raise VaultError("LIVE 接口凭据不完整")
    try:
        rpc = urlsplit(settings["rpc_url"])
    except (TypeError, ValueError):
        raise VaultError("Polygon RPC 地址无效") from None
    if rpc.scheme != "https" or not rpc.hostname:
        raise VaultError("Polygon RPC 地址须使用 HTTPS")
    if type(settings["auto_approve_redemption"]) is not bool:
        raise VaultError("自动赎回设置无效")
    return {"signer": signer, "funder": funder, "signature_type": signature_type}


def _validate_password(password: str) -> bytes:
    if not isinstance(password, str) or not 12 <= len(password) <= 4096:
        raise VaultError("解锁密码至少需要 12 个字符")
    return password.encode("utf-8")


def _derive_key(password: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password,
        salt=salt,
        n=_KDF_N,
        r=_KDF_R,
        p=_KDF_P,
        dklen=32,
        maxmem=_KDF_MAXMEM,
    )


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str, length: int | None = None) -> bytes:
    if not isinstance(value, str):
        raise VaultError("加密钱包文件无效")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise VaultError("加密钱包文件无效") from None
    if length is not None and len(raw) != length:
        raise VaultError("加密钱包文件无效")
    return raw


def _authenticated_header(envelope: dict) -> bytes:
    header = {key: envelope[key] for key in ("format", "version", "metadata", "kdf", "aead")}
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _seal(settings: dict, password: str) -> tuple[bytes, dict]:
    metadata = _validate_settings(settings)
    key = _derive_key(_validate_password(password), salt := secrets.token_bytes(16))
    nonce = secrets.token_bytes(12)
    envelope = {
        "format": _FORMAT,
        "version": _VERSION,
        "metadata": metadata,
        "kdf": {"name": "scrypt", "n": _KDF_N, "r": _KDF_R, "p": _KDF_P, "salt": _b64(salt)},
        "aead": {"name": "AES-256-GCM", "nonce": _b64(nonce)},
    }
    plaintext = bytearray(json.dumps(settings, separators=(",", ":")).encode("utf-8"))
    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)
        cipher.update(_authenticated_header(envelope))
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    finally:
        plaintext[:] = b"\x00" * len(plaintext)
    envelope["ciphertext"] = _b64(ciphertext)
    envelope["tag"] = _b64(tag)
    output = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(output) > _MAX_FILE:
        raise VaultError("加密钱包文件大小异常")
    return output, metadata


def _open_private_dir(path: Path, *, create: bool) -> int:
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError:
            raise VaultError("无法安全创建加密钱包目录") from None
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise VaultError("加密钱包目录须由运行用户持有且权限为 0700")
        return fd
    except OSError:
        if fd is not None:
            os.close(fd)
        raise VaultError("无法安全访问加密钱包目录") from None
    except Exception:
        if fd is not None:
            os.close(fd)
        raise


def write_vault(path: str | os.PathLike, settings: dict, password: str) -> dict:
    """Create a 0600 AES-GCM vault atomically; refuse to replace any prior entry."""
    payload, metadata = _seal(settings, password)
    target = Path(path)
    if target.name in {"", ".", ".."}:
        raise VaultError("加密钱包文件路径无效")
    dir_fd = _open_private_dir(target.parent, create=True)
    temporary = f".{target.name}.{secrets.token_hex(16)}.tmp"
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(
                    temporary,
                    target.name,
                    src_dir_fd=dir_fd,
                    dst_dir_fd=dir_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise VaultError("加密钱包文件已存在；请先完成备份和替换流程") from None
        finally:
            os.unlink(temporary, dir_fd=dir_fd)
            os.fsync(dir_fd)
    except VaultError:
        raise
    except OSError:
        raise VaultError("无法安全写入加密钱包文件") from None
    finally:
        os.close(dir_fd)
    return metadata


def _read_vault_bytes(path: str | os.PathLike) -> bytes:
    target = Path(path)
    dir_fd = _open_private_dir(target.parent, create=False)
    try:
        fd = os.open(
            target.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=dir_fd,
        )
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > _MAX_FILE
            ):
                raise VaultError("加密钱包文件权限、属主或类型无效")
            return stream.read(_MAX_FILE + 1)
    except VaultError:
        raise
    except OSError:
        raise VaultError("无法安全读取加密钱包文件") from None
    finally:
        os.close(dir_fd)


def _parse_envelope(path: str | os.PathLike) -> dict:
    raw = _read_vault_bytes(path)
    if len(raw) > _MAX_FILE:
        raise VaultError("加密钱包文件大小异常")
    try:
        envelope = json.loads(raw)
    except (UnicodeError, ValueError):
        raise VaultError("加密钱包文件无效") from None
    if not isinstance(envelope, dict) or set(envelope) != {
        "format",
        "version",
        "metadata",
        "kdf",
        "aead",
        "ciphertext",
        "tag",
    }:
        raise VaultError("加密钱包文件无效")
    if (
        envelope["format"] != _FORMAT
        or type(envelope["version"]) is not int
        or envelope["version"] != _VERSION
    ):
        raise VaultError("加密钱包文件版本无效")
    metadata = envelope["metadata"]
    if not isinstance(metadata, dict) or set(metadata) != _METADATA_KEYS:
        raise VaultError("加密钱包文件无效")
    if (
        not isinstance(metadata["signer"], str)
        or not isinstance(metadata["funder"], str)
        or type(metadata["signature_type"]) is not int
    ):
        raise VaultError("加密钱包文件无效")
    kdf = envelope["kdf"]
    if not isinstance(kdf, dict) or set(kdf) != {"name", "n", "r", "p", "salt"}:
        raise VaultError("加密钱包文件无效")
    if (
        kdf["name"] != "scrypt"
        or type(kdf["n"]) is not int
        or type(kdf["r"]) is not int
        or type(kdf["p"]) is not int
        or (kdf["n"], kdf["r"], kdf["p"]) != (_KDF_N, _KDF_R, _KDF_P)
    ):
        raise VaultError("加密钱包文件参数无效")
    aead = envelope["aead"]
    if (
        not isinstance(aead, dict)
        or set(aead) != {"name", "nonce"}
        or aead["name"] != "AES-256-GCM"
    ):
        raise VaultError("加密钱包文件参数无效")
    _unb64(kdf["salt"], 16)
    _unb64(aead["nonce"], 12)
    _unb64(envelope["tag"], 16)
    _unb64(envelope["ciphertext"])
    return envelope


def read_public_metadata(path: str | os.PathLike) -> dict:
    """Peek public identity. This is UNVERIFIED until ``read_vault`` succeeds."""
    return dict(_parse_envelope(path)["metadata"])


def read_vault(path: str | os.PathLike, password: str) -> dict:
    """Unlock a vault; reject a wrong password, modification, or identity mismatch."""
    envelope = _parse_envelope(path)
    salt = _unb64(envelope["kdf"]["salt"], 16)
    nonce = _unb64(envelope["aead"]["nonce"], 12)
    key = _derive_key(_validate_password(password), salt)
    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)
        cipher.update(_authenticated_header(envelope))
        plaintext = cipher.decrypt_and_verify(
            _unb64(envelope["ciphertext"]), _unb64(envelope["tag"], 16)
        )
        settings = json.loads(plaintext)
        if _validate_settings(settings) != envelope["metadata"]:
            raise ValueError("identity mismatch")
    except Exception:
        raise VaultAuthError("钱包解锁失败；密码不正确或文件已损坏") from None
    return settings
