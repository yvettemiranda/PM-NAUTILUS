"""Local credential-file and error-display checks; no usable key or network access."""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from pm_nautilus import live
from pm_nautilus.redemption import RedemptionCheckError, RedemptionService


SETTINGS = {
    "private_key": "0x" + "11" * 32,
    "api_key": "not-an-api-key",
    "api_secret": "not-an-api-secret",
    "passphrase": "not-a-passphrase",
    "funder": "0x" + "22" * 20,
    "rpc_url": "https://example.invalid",
    "signature_type": 2,
}


def credentials_file(tmp_path, monkeypatch, value=SETTINGS):
    path = tmp_path / "live.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    monkeypatch.setenv("PM_LIVE_CREDENTIALS_FILE", str(path))
    monkeypatch.setenv("PM_LIVE_ENABLED", "true")
    return path


def test_live_credentials_default_gate_and_explicit_preflight(tmp_path, monkeypatch):
    credentials_file(tmp_path, monkeypatch)
    monkeypatch.setenv("PM_LIVE_ENABLED", "false")
    with pytest.raises(ValueError, match="未由服务器显式启用"):
        live.load_settings()
    assert live.load_settings(require_live_enabled=False) == SETTINGS


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400])
def test_live_credentials_require_exact_0600(tmp_path, monkeypatch, mode):
    path = credentials_file(tmp_path, monkeypatch)
    path.chmod(mode)
    with pytest.raises(ValueError, match="0600"):
        live.load_settings()


def test_live_credentials_reject_symlink_and_nonregular_file(tmp_path, monkeypatch):
    target = credentials_file(tmp_path, monkeypatch)
    link = tmp_path / "link.json"
    link.symlink_to(target)
    monkeypatch.setenv("PM_LIVE_CREDENTIALS_FILE", str(link))
    with pytest.raises(ValueError) as exc:
        live.load_settings()
    assert str(link) not in str(exc.value)

    monkeypatch.setenv("PM_LIVE_CREDENTIALS_FILE", str(tmp_path))
    with pytest.raises(ValueError):
        live.load_settings()


def test_live_credentials_reject_unexpected_owner(tmp_path, monkeypatch):
    credentials_file(tmp_path, monkeypatch)
    real_fstat = os.fstat
    monkeypatch.setattr(live.os, "geteuid", lambda: 99_998)

    def foreign_fstat(fd):
        info = real_fstat(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=99_999)

    monkeypatch.setattr(live.os, "fstat", foreign_fstat)
    with pytest.raises(ValueError, match="UID10001"):
        live.load_settings()


def test_live_credentials_parse_errors_do_not_reveal_contents(tmp_path, monkeypatch):
    path = credentials_file(tmp_path, monkeypatch)
    marker = "SECRET_MARKER_DO_NOT_EXPOSE"
    path.write_text('{"private_key": "' + marker + '",')
    with pytest.raises(ValueError) as exc:
        live.load_settings()
    assert marker not in str(exc.value)
    assert str(path) not in str(exc.value)


def test_live_credentials_reject_bad_key_and_rpc_without_echo(tmp_path, monkeypatch):
    marker = "SECRET_MARKER_DO_NOT_EXPOSE"
    for changes in ({"private_key": marker}, {"rpc_url": marker}):
        credentials_file(tmp_path, monkeypatch, SETTINGS | changes)
        with pytest.raises(ValueError) as exc:
            live.load_settings()
        assert marker not in str(exc.value)


class Store:
    generation = 1

    def __init__(self):
        self.saved = []

    def put(self, key, value):
        self.saved.append((key, value))


def test_redemption_external_value_error_cannot_enter_persisted_state():
    marker = "SECRET_MARKER_DO_NOT_EXPOSE"
    claim = {"generation": 1, "state": "IDENTIFIED"}
    store = Store()
    runtime = SimpleNamespace(
        mode="LIVE", store=store, business={"cycles": {}, "claims": {"claim": claim}}
    )

    class Wallet:
        def prepare(self, _claim):
            raise ValueError(f"RPC rejected secret={marker}")

    asyncio.run(RedemptionService(runtime, None, Wallet()).run_once())
    assert claim["error"] == "ValueError"
    assert marker not in repr(store.saved)


def test_redemption_application_check_keeps_its_static_guidance():
    claim = {"generation": 1, "state": "IDENTIFIED"}
    runtime = SimpleNamespace(
        mode="LIVE", store=Store(), business={"cycles": {}, "claims": {"claim": claim}}
    )

    class Wallet:
        def prepare(self, _claim):
            raise RedemptionCheckError("链上尚未正式结算")

    asyncio.run(RedemptionService(runtime, None, Wallet()).run_once())
    assert claim["error"] == "链上尚未正式结算"
