"""Exercise the credential bundle's local checks without real secrets or age."""

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def bundle_module():
    path = Path(__file__).resolve().parents[1] / "deploy" / "live-secrets.py"
    spec = importlib.util.spec_from_file_location("live_secrets_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_derived_api_credentials_use_official_client_without_printing_response(capsys):
    module = bundle_module()
    calls = []

    class Client:
        def __init__(self, host, *, chain_id, key, signature_type, funder):
            calls.append((host, chain_id, key, signature_type, funder))

        def create_or_derive_api_key(self):
            return SimpleNamespace(
                api_key="controlled-key",
                api_secret="controlled-secret",
                api_passphrase="controlled-passphrase",
            )

    result = module.derive_api_credentials("0x" + "11" * 32, "0x" + "22" * 20, 2, Client)
    assert result == {
        "api_key": "controlled-key",
        "api_secret": "controlled-secret",
        "passphrase": "controlled-passphrase",
    }
    assert calls == [("https://clob.polymarket.com", 137, "0x" + "11" * 32, 2, "0x" + "22" * 20)]
    assert capsys.readouterr() == ("", "")


def test_api_error_hides_third_party_secret(capsys):
    module = bundle_module()

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def create_or_derive_api_key(self):
            raise RuntimeError("SECRET_MARKER_DO_NOT_EXPOSE")

    with pytest.raises(module.SetupError) as exc:
        module.derive_api_credentials("0x" + "11" * 32, "0x" + "22" * 20, 2, Client)
    assert "SECRET_MARKER" not in str(exc.value)
    assert capsys.readouterr() == ("", "")


def test_bundle_write_is_exclusive_and_0600(tmp_path):
    module = bundle_module()
    target = tmp_path / "vault.age"
    module.write_new_file(target, b"first encrypted payload", os.geteuid(), os.getegid())
    assert target.read_bytes() == b"first encrypted payload"
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        module.write_new_file(target, b"second payload", os.geteuid(), os.getegid())
    assert target.read_bytes() == b"first encrypted payload"
    assert list(tmp_path.iterdir()) == [target]
