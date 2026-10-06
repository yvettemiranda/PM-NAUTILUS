"""Offline wallet import and encrypted credential vault checks."""

import json
import os
import stat

import pytest

from pm_nautilus.wallet_vault import (
    VaultAuthError,
    VaultError,
    derive_signer,
    read_public_metadata,
    read_vault,
    write_vault,
)


MNEMONIC = "abandon " * 11 + "about"  # Public BIP39 test vector; never fund this account.
PASSWORD = "a long unique vault password"


def settings():
    signer = derive_signer("private_key", "0x" + "11" * 32)
    return {
        "private_key": signer.private_key,
        "api_key": "fake-api-key-for-offline-test",
        "api_secret": "fake-api-secret-for-offline-test",
        "passphrase": "fake-api-passphrase-for-offline-test",
        "funder": signer.address,
        "rpc_url": "https://polygon-rpc.example.invalid",
        "signature_type": 0,
        "auto_approve_redemption": False,
    }


def test_bip39_vector_and_address_index_match_ethereum_default_path():
    first = derive_signer("mnemonic", MNEMONIC)
    second = derive_signer("mnemonic", MNEMONIC, account_index=1)
    assert first.address == "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    assert first.private_key == "0x1ab42cc412b618bdea3a599e3c9bae199ebf030895b039e9db1e30dafb12b727"
    assert second.address == "0x6Fac4D18c912343BF86fa7049364Dd4E424Ab9C0"
    assert (
        derive_signer("mnemonic", MNEMONIC, mnemonic_passphrase="different").address
        != first.address
    )
    assert first.private_key not in repr(first)


@pytest.mark.parametrize(
    "secret",
    ["abandon " * 12, "abandon " * 11 + "zebra", "abandon " * 10 + "about"],
)
def test_mnemonic_count_words_and_checksum_rejected_without_echo(secret):
    with pytest.raises(VaultError) as exc:
        derive_signer("mnemonic", secret)
    assert secret not in str(exc.value)


def test_private_key_and_index_validation():
    signer = derive_signer("private_key", "0x" + "11" * 32)
    assert signer.address == "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
    assert signer.private_key == "0x" + "11" * 32
    with pytest.raises(VaultError):
        derive_signer("private_key", "0x" + "00" * 32)
    with pytest.raises(VaultError):
        derive_signer("mnemonic", MNEMONIC, account_index=True)
    with pytest.raises(VaultError):
        derive_signer("mnemonic", MNEMONIC, account_index=-1)


def test_vault_roundtrip_metadata_and_encrypted_at_rest(tmp_path):
    path = tmp_path / "wallet" / "live.vault"
    source = settings()
    metadata = write_vault(path, source, PASSWORD)
    assert metadata == {
        "signer": source["funder"],
        "funder": source["funder"],
        "signature_type": 0,
    }
    assert read_public_metadata(path) == metadata
    assert read_vault(path, PASSWORD) == source
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_bytes()
    for item in (source["private_key"], source["api_key"], source["api_secret"], PASSWORD):
        assert item.encode() not in raw
    assert set(json.loads(raw)["metadata"]) == {"signer", "funder", "signature_type"}


def test_wrong_password_and_ciphertext_tampering_fail_same_way(tmp_path):
    path = tmp_path / "wallet" / "live.vault"
    write_vault(path, settings(), PASSWORD)
    with pytest.raises(VaultAuthError, match="密码不正确或文件已损坏"):
        read_vault(path, "a different long password")

    envelope = json.loads(path.read_text())
    envelope["metadata"]["signer"] = "0x" + "22" * 20
    path.write_text(json.dumps(envelope))
    with pytest.raises(VaultAuthError):
        read_vault(path, PASSWORD)


def test_vault_refuses_overwrite_and_symlink(tmp_path):
    directory = tmp_path / "wallet"
    path = directory / "live.vault"
    write_vault(path, settings(), PASSWORD)
    before = path.read_bytes()
    with pytest.raises(VaultError, match="已存在"):
        write_vault(path, settings(), PASSWORD)
    assert path.read_bytes() == before

    link = directory / "link.vault"
    link.symlink_to(path)
    with pytest.raises(VaultError):
        read_vault(link, PASSWORD)
    with pytest.raises(VaultError, match="已存在"):
        write_vault(link, settings(), PASSWORD)
    assert path.read_bytes() == before


def test_vault_rejects_weak_permissions_and_untrusted_directory(tmp_path):
    path = tmp_path / "wallet" / "live.vault"
    write_vault(path, settings(), PASSWORD)
    path.chmod(0o644)
    with pytest.raises(VaultError, match="权限"):
        read_vault(path, PASSWORD)
    path.chmod(0o600)
    path.parent.chmod(0o755)
    with pytest.raises(VaultError, match="0700"):
        read_vault(path, PASSWORD)


def test_vault_rejects_seed_fields_and_mismatched_eoa(tmp_path):
    path = tmp_path / "wallet" / "live.vault"
    data = settings() | {"mnemonic": MNEMONIC}
    with pytest.raises(VaultError, match="不支持的字段"):
        write_vault(path, data, PASSWORD)
    assert not path.exists()

    data = settings() | {"funder": "0x" + "22" * 20}
    with pytest.raises(VaultError, match="必须一致"):
        write_vault(path, data, PASSWORD)
    assert not path.exists()


def test_password_must_be_long_and_is_not_returned(tmp_path):
    path = tmp_path / "wallet" / "live.vault"
    with pytest.raises(VaultError, match="至少需要 12"):
        write_vault(path, settings(), "short")
    assert not path.exists()
    write_vault(path, settings(), PASSWORD)
    assert PASSWORD not in repr(read_public_metadata(path))


def test_vault_rejects_fifo_without_blocking(tmp_path):
    directory = tmp_path / "wallet"
    directory.mkdir(mode=0o700)
    fifo = directory / "fifo.vault"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(VaultError, match="权限"):
        read_vault(fifo, PASSWORD)
