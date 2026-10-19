"""Offline compaction preserves both ledgers and refuses a running application."""

import fcntl
import json
import sqlite3
from copy import deepcopy
from dataclasses import asdict
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from pm_nautilus.rules import Fees, Token
from pm_nautilus.store import Store
from pm_nautilus.strategy import Runtime
from test_runtime import setup

script = Path(__file__).resolve().parents[1] / "scripts" / "compact_metadata.py"
spec = spec_from_file_location("compact_metadata", script)
compactor = module_from_spec(spec)
spec.loader.exec_module(compactor)


@pytest.fixture
def legacy_pair(tmp_path):
    root = tmp_path / "runtime"
    token = Token(
        "1",
        "event",
        "market",
        "condition",
        "YES",
        2,
        False,
        0,
        10**15,
        Fees(False),
        5_000_000,
        1000,
    )
    for mode in ("TEST", "LIVE"):
        path = root / mode / "state.sqlite"
        Store(path, mode).close()
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO tokens VALUES(?,?)", (token.token_id, json.dumps(asdict(token)))
            )
            db.execute("UPDATE meta SET value=? WHERE key='status'", (json.dumps("RUNNING"),))
            db.execute("CREATE TABLE extra_state(k TEXT PRIMARY KEY,v BLOB,n REAL)")
            db.execute("INSERT INTO extra_state VALUES('key',?,1.25)", (b"\x00\xff",))
    return root


def fingerprints(root):
    result = {}
    for mode in ("TEST", "LIVE"):
        with sqlite3.connect(root / mode / "state.sqlite") as db:
            result[mode] = compactor.logical_fingerprints(db)
    return result


@pytest.mark.parametrize("vacuum", [False, True])
def test_compaction_keeps_all_rows_status_and_private_rollback_copies(
    legacy_pair, tmp_path, vacuum
):
    root = legacy_pair
    before = fingerprints(root)
    backup = tmp_path / "rollback"
    report = compactor.compact_metadata(root, backup, vacuum=vacuum)
    assert report["status"] == "verified"
    assert fingerprints(root) == before
    assert backup.stat().st_mode & 0o777 == 0o700
    for mode in ("TEST", "LIVE"):
        assert report["modes"][mode]["tokens"]["changed"] == 1
        path = backup / f"{mode}.sqlite"
        assert path.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(path) as db:
            assert compactor.logical_fingerprints(db) == before[mode]
            raw = json.loads(db.execute("SELECT payload FROM tokens").fetchone()[0])
            assert "token_id" in raw  # Rollback copy is still readable by the old application.
        reopened = Store(root / mode / "state.sqlite", mode)
        assert reopened.tokens()["1"].event_id == "event"
        reopened.close()
    assert (backup / "manifest.json").stat().st_mode & 0o777 == 0o600


def test_running_application_refused_before_backup_or_mutation(legacy_pair, tmp_path):
    root = legacy_pair
    before = fingerprints(root)
    backup = tmp_path / "rollback"
    with (root / "process.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="application is running"):
            compactor.compact_metadata(root, backup)
    assert not backup.exists()
    assert fingerprints(root) == before


def test_non_token_change_is_detected_and_whole_mode_transaction_rolls_back(
    legacy_pair, tmp_path, monkeypatch
):
    root = legacy_pair
    before = fingerprints(root)
    original = Store.compact_tokens

    def corrupt(db):
        result = original(db)
        db.execute("UPDATE extra_state SET v=?", (b"changed",))
        return result

    monkeypatch.setattr(Store, "compact_tokens", staticmethod(corrupt))
    backup = tmp_path / "rollback"
    with pytest.raises(ValueError, match="logical data changed"):
        compactor.compact_metadata(root, backup)
    assert fingerprints(root) == before
    report = json.loads((backup / "manifest.json").read_text())
    assert report["status"] == "incomplete"
    assert all(item["status"] == "backed_up" for item in report["modes"].values())


def test_existing_backup_is_never_overwritten(legacy_pair, tmp_path):
    root = legacy_pair
    before = fingerprints(root)
    backup = tmp_path / "rollback"
    backup.mkdir()
    sentinel = backup / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(FileExistsError):
        compactor.compact_metadata(root, backup)
    assert sentinel.read_text() == "keep"
    assert fingerprints(root) == before


def test_wrong_mode_rejected_before_backup_or_compaction(legacy_pair, tmp_path):
    root = legacy_pair
    with sqlite3.connect(root / "LIVE" / "state.sqlite") as db:
        db.execute("UPDATE meta SET value='\"TEST\"' WHERE key='mode'")
    before = fingerprints(root)
    backup = tmp_path / "rollback"
    with pytest.raises(ValueError, match="mode mismatch"):
        compactor.compact_metadata(root, backup)
    assert not backup.exists()
    assert fingerprints(root) == before


def test_native_replay_and_consumed_test_depth_survive_offline_compaction(tmp_path):
    root = tmp_path / "runtime"
    runtime, clock, token = setup(root / "TEST")
    bids, asks = [(15000, 100_000_000)], [(20000, 50_000_000)]
    runtime.book("1", bids, asks)
    runtime.start()
    runtime.pause()
    expected_cash = runtime.cash()
    expected_business = deepcopy(runtime.business)
    expected_depth = runtime.books["1"].ask.available()
    expected_generation = runtime.store.generation
    assert runtime.validate()["ok"] and runtime.store.events()
    runtime.close()
    # setup uses test.sqlite; the deployed tool operates on state.sqlite.
    (root / "TEST" / "test.sqlite").rename(root / "TEST" / "state.sqlite")
    live = Store(root / "LIVE" / "state.sqlite", "LIVE")
    live.save_token(token)
    live.close()
    for mode in ("TEST", "LIVE"):
        with sqlite3.connect(root / mode / "state.sqlite") as db:
            rows = db.execute("SELECT token_id,payload FROM tokens").fetchall()
            for token_id, payload in rows:
                db.execute(
                    "UPDATE tokens SET payload=? WHERE token_id=?",
                    (json.dumps(Store.decode_token_payload(payload)), token_id),
                )
    compactor.compact_metadata(root, tmp_path / "rollback", vacuum=True)
    restored = Runtime(root / "TEST" / "state.sqlite", clock=clock)
    try:
        assert restored.cash() == expected_cash
        assert restored.business == expected_business
        assert restored.store.generation == expected_generation
        assert not restored.books["1"].ready
        restored.book("1", bids, asks)
        assert restored.books["1"].ask.available() == expected_depth
        assert restored.validate()["ok"]
    finally:
        restored.close()
