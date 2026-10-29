"""Execution intent is strict, durable and independent from trading state."""

import json
import os
import stat
from unittest.mock import Mock

import pytest

import pm_nautilus.execution_mode as module
from pm_nautilus.execution_mode import ExecutionModeStore


def test_missing_intent_defaults_test_without_creating_files(tmp_path):
    root = tmp_path / "not-created"
    store = ExecutionModeStore(root)
    assert store.read() == "TEST"
    assert not root.exists()


def test_live_intent_survives_new_instance_and_can_explicitly_return_to_test(tmp_path):
    store = ExecutionModeStore(tmp_path)
    store.save("LIVE")
    assert ExecutionModeStore(tmp_path).read() == "LIVE"
    assert json.loads(store.path.read_bytes()) == {"version": 1, "mode": "LIVE"}
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert [path.name for path in tmp_path.iterdir()] == [store.FILENAME]
    store.save("TEST")
    assert ExecutionModeStore(tmp_path).read() == "TEST"


@pytest.mark.parametrize("mode", [None, True, 1, "live", "PAPER", "", "TEST\n", {}])
def test_save_rejects_unknown_modes_before_creating_any_file(tmp_path, mode):
    store = ExecutionModeStore(tmp_path / "not-created")
    with pytest.raises(ValueError):
        store.save(mode)
    assert not store.root.exists()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not JSON",
        b"\xff",
        b"[]",
        b"null",
        b'{"version":1,"mode":"UNKNOWN"}',
        b'{"version":2,"mode":"TEST"}',
        b'{"version":true,"mode":"TEST"}',
        b'{"version":1.0,"mode":"TEST"}',
        b'{"version":"1","mode":"TEST"}',
        b'{"version":1,"mode":false}',
        b'{"version":1,"mode":"TEST","wallet":"not allowed"}',
        b'{"version":1,"mode":"TEST","mode":"LIVE"}',
        b'{"version":1,"version":1,"mode":"TEST"}',
        b'{"version":1}',
        b'{"mode":"LIVE"}',
        b'{"version":NaN,"mode":"TEST"}',
        b" " * 257,
    ],
)
def test_malformed_or_oversized_existing_intent_fails_closed(tmp_path, raw):
    store = ExecutionModeStore(tmp_path)
    store.path.write_bytes(raw)
    with pytest.raises(ValueError):
        store.read()
    with pytest.raises(ValueError):
        store.save("TEST")
    assert store.path.read_bytes() == raw
    assert [path.name for path in tmp_path.iterdir()] == [store.FILENAME]


@pytest.mark.parametrize("dangling", [False, True])
def test_mode_symlink_is_never_followed_or_replaced(tmp_path, dangling):
    root = tmp_path / "root"
    root.mkdir()
    victim = tmp_path / "other.json"
    raw = b'{"version":1,"mode":"LIVE"}'
    if not dangling:
        victim.write_bytes(raw)
    store = ExecutionModeStore(root)
    store.path.symlink_to(victim)
    with pytest.raises(ValueError):
        store.read()
    with pytest.raises(ValueError):
        store.save("TEST")
    assert store.path.is_symlink()
    assert victim.read_bytes() == raw if not dangling else not victim.exists()


def test_root_symlink_is_refused(tmp_path):
    root = tmp_path / "real"
    root.mkdir()
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    store = ExecutionModeStore(link)
    with pytest.raises(ValueError):
        store.read()
    with pytest.raises(ValueError):
        store.save("LIVE")
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_nonregular_mode_file_is_refused_without_blocking(tmp_path, kind):
    store = ExecutionModeStore(tmp_path)
    if kind == "directory":
        store.path.mkdir()
    else:
        os.mkfifo(store.path)
    with pytest.raises(ValueError):
        store.read()
    with pytest.raises(ValueError):
        store.save("LIVE")


@pytest.mark.parametrize("failure", ["file-fsync", "replace"])
@pytest.mark.parametrize("existing", [False, True])
def test_pre_replace_failure_preserves_previous_intent_and_removes_temporary_file(
    tmp_path, monkeypatch, failure, existing
):
    store = ExecutionModeStore(tmp_path)
    if existing:
        store.save("LIVE")
    previous = store.path.read_bytes() if existing else None
    failed = Mock(side_effect=OSError("controlled atomic save failure"))
    monkeypatch.setattr(module.os, "fsync" if failure == "file-fsync" else "replace", failed)
    with pytest.raises(OSError, match="controlled atomic"):
        store.save("TEST")
    assert (store.path.read_bytes() if store.path.exists() else None) == previous
    assert store.read() == ("LIVE" if existing else "TEST")
    assert list(tmp_path.glob(".execution-mode-*.tmp")) == []


def test_directory_fsync_after_replace_raises_without_claiming_durability_or_rolling_back(
    tmp_path, monkeypatch
):
    store = ExecutionModeStore(tmp_path)
    store.save("TEST")
    original_fsync = os.fsync
    original_replace = os.replace
    calls = []

    def fsync(descriptor):
        directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        calls.append("directory-fsync" if directory else "file-fsync")
        if directory:
            raise OSError("controlled post-replace durability uncertainty")
        return original_fsync(descriptor)

    def replace(*args, **kwargs):
        calls.append("replace")
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(module.os, "fsync", fsync)
    monkeypatch.setattr(module.os, "replace", replace)
    with pytest.raises(OSError, match="post-replace"):
        store.save("LIVE")
    assert calls == ["file-fsync", "replace", "directory-fsync"]
    assert store.read() == "LIVE"  # Visible, but save did not confirm durability.
    assert list(tmp_path.glob(".execution-mode-*.tmp")) == []
