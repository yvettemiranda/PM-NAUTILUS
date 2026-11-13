"""deploy/offsite-backup.py: consistent online ledger copies, manifest, safety guards."""

import hashlib
import importlib.util
import json
import os
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "offsite-backup.py"
spec = importlib.util.spec_from_file_location("offsite_backup", SCRIPT)
offsite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offsite)


def ledger(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE t(i INTEGER PRIMARY KEY, v BLOB)")
    db.executemany("INSERT INTO t VALUES(?, ?)", [(i, os.urandom(64)) for i in range(rows)])
    db.commit()
    return db  # kept open: the app holds its ledgers while the backup runs


@pytest.fixture
def app_dir(tmp_path):
    app = tmp_path / "app"
    data = app / "runtime" / "server"
    open_ledgers = [
        ledger(data / "TEST" / "state.sqlite", 300),
        ledger(data / "TEST" / "accounts" / "2" / "state.sqlite", 200),
        ledger(data / "LIVE" / "state.sqlite", 10),
    ]
    # Uncommitted-to-file pages live only in the WAL until a checkpoint.
    open_ledgers[0].execute("INSERT INTO t VALUES(10000, x'01')")
    open_ledgers[0].commit()
    (data / "execution-mode.json").write_text('{"mode": "TEST"}')
    (data / "wallet").mkdir()
    (data / "wallet" / "live.vault").write_bytes(b"encrypted")
    (data / "process.lock").write_text("")
    (data / "LIVE" / ".pm-live-upload-abc.pmnb").write_text("scratch")
    # In-flight wallet export/restore work areas: whole directories are scratch.
    export = data / "LIVE" / ".pm-live-export-x1"
    export.mkdir()
    (export / "snapshot.sqlite").write_bytes(b"half-written, not a database")
    (export / "bundle.pmnb").write_bytes(b"partial")
    restore = data / "LIVE" / ".pm-live-restore-y2" / "nested"
    restore.mkdir(parents=True)
    (restore / "payload.bin").write_bytes(b"decrypted bundle")
    (restore / "restored.sqlite").write_bytes(b"partial")
    (app / ".env").write_text("PM_GIT_REVISION=" + "c" * 40 + "\n")
    yield app
    for db in open_ledgers:
        db.close()


def test_archive_contains_consistent_ledgers_and_durable_files_only(app_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("PM_APP_DIR", str(app_dir))
    monkeypatch.setenv("PM_OFFSITE_STAGING", str(tmp_path / "staging"))
    assert offsite.main(["--no-upload"]) == 0
    (archive,) = (tmp_path / "staging").glob("pm-nautilus-*.tar.gz")
    assert oct(archive.stat().st_mode & 0o777) == "0o600"
    out = tmp_path / "out"
    with tarfile.open(archive) as stream:
        stream.extractall(out, filter="data")
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["revision"] == "c" * 40
    paths = {item["path"]: item for item in manifest["files"]}
    assert set(paths) == {
        "LIVE/state.sqlite",
        "TEST/accounts/2/state.sqlite",
        "TEST/state.sqlite",
        "execution-mode.json",
        "wallet/live.vault",
    }
    for relative, item in paths.items():
        data = (out / "data" / relative).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
    copy = sqlite3.connect(out / "data" / "TEST" / "state.sqlite")
    assert copy.execute("SELECT count(*) FROM t").fetchone()[0] == 301  # includes WAL rows
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert not list((out / "data").rglob("*-wal")) and not list((out / "data").rglob("*-shm"))


def test_upload_uses_coscli_and_local_archives_are_pruned(app_dir, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(offsite.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setenv("PM_APP_DIR", str(app_dir))
    monkeypatch.setenv("PM_OFFSITE_STAGING", str(tmp_path / "staging"))
    monkeypatch.setenv("PM_OFFSITE_COS_URI", "cos://bucket-1250000000/pm-nautilus/")
    monkeypatch.setenv("PM_OFFSITE_COSCLI_CONFIG", "/etc/pm-nautilus/cos.yaml")
    monkeypatch.setenv("PM_OFFSITE_KEEP_LOCAL", "1")
    for second in range(2):
        monkeypatch.setattr(
            offsite.time, "gmtime", lambda s=second: (2026, 11, 2, 4, 17, s, 0, 0, 0)
        )
        assert offsite.main([]) == 0
    assert len(list((tmp_path / "staging").glob("pm-nautilus-*.tar.gz"))) == 1
    assert calls[-1][:2] == ["coscli", "cp"]
    assert calls[-1][3].startswith("cos://bucket-1250000000/pm-nautilus/pm-nautilus-")
    assert calls[-1][-2:] == ["-c", "/etc/pm-nautilus/cos.yaml"]


def test_refuses_to_run_as_a_different_owner(app_dir, tmp_path, monkeypatch):
    other = os.geteuid() + 1
    monkeypatch.setattr(offsite.os, "geteuid", lambda: other)
    with pytest.raises(RuntimeError, match="ledger owner"):
        offsite.build_archive(app_dir, tmp_path / "staging")


def test_mixed_ledger_owners_abort_before_any_copy(app_dir, tmp_path, monkeypatch):
    foreign_ledger = app_dir / "runtime" / "server" / "LIVE" / "state.sqlite"
    original_stat = Path.stat

    def mixed_owner(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == foreign_ledger:
            return SimpleNamespace(st_uid=os.geteuid() + 1, st_mode=result.st_mode)
        return result

    def unexpected_copy(*args, **kwargs):
        pytest.fail("mixed ledger owners must be rejected before opening any SQLite copy")

    monkeypatch.setattr(Path, "stat", mixed_owner)
    monkeypatch.setattr(offsite, "copy_sqlite", unexpected_copy)
    with pytest.raises(RuntimeError, match="one consistent ledger owner"):
        offsite.build_archive(app_dir, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def test_refuses_an_empty_data_directory_and_bad_destination(tmp_path):
    (tmp_path / "app" / "runtime" / "server").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="no ledgers"):
        offsite.build_archive(tmp_path / "app", tmp_path / "staging")
    with pytest.raises(RuntimeError, match="cos://"):
        offsite.upload(tmp_path / "x.tar.gz", "s3://bucket/", "coscli", None)


def test_scratch_paths_are_skipped_at_any_depth():
    assert offsite.skipped(Path("LIVE/.pm-live-export-x/snapshot.sqlite"))
    assert offsite.skipped(Path("LIVE/.pm-live-restore-y/a/b/payload.bin"))
    assert offsite.skipped(Path("TEST/state.sqlite-wal"))
    assert not offsite.skipped(Path("LIVE/state.sqlite"))
    assert not offsite.skipped(Path("wallet/live.vault"))


def test_failed_uploads_still_keep_local_archives_bounded(app_dir, tmp_path, monkeypatch):
    def fail(cmd, **kw):
        raise offsite.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(offsite.subprocess, "run", fail)
    monkeypatch.setenv("PM_APP_DIR", str(app_dir))
    monkeypatch.setenv("PM_OFFSITE_STAGING", str(tmp_path / "staging"))
    monkeypatch.setenv("PM_OFFSITE_COS_URI", "cos://bucket-1250000000/pm-nautilus/")
    monkeypatch.setenv("PM_OFFSITE_KEEP_LOCAL", "1")
    for second in range(3):
        monkeypatch.setattr(
            offsite.time, "gmtime", lambda s=second: (2026, 11, 2, 4, 17, s, 0, 0, 0)
        )
        with pytest.raises(offsite.subprocess.CalledProcessError):
            offsite.main([])
    assert len(list((tmp_path / "staging").glob("pm-nautilus-*.tar.gz"))) == 1


def test_failed_archive_leaves_no_partial_and_old_partials_are_removed(
    app_dir, tmp_path, monkeypatch
):
    staging = tmp_path / "staging"
    staging.mkdir()
    stale = staging / ".pm-nautilus-host-20260101T000000Z.tar.gz.partial"
    stale.write_bytes(b"interrupted")

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(offsite.tarfile.TarFile, "add", broken)
    with pytest.raises(OSError, match="disk full"):
        offsite.build_archive(app_dir, staging)
    assert not list(staging.glob("*.partial")) and not list(staging.glob(".*.partial"))
