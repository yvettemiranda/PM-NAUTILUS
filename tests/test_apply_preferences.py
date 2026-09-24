"""A public profile can seed only two pristine, stopped ledgers."""

import fcntl
import json
import sqlite3
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from pm_nautilus.config import Preferences
from pm_nautilus.store import Store

script = Path(__file__).resolve().parents[1] / "scripts" / "apply_preferences.py"
spec = spec_from_file_location("apply_preferences", script)
importer = module_from_spec(spec)
spec.loader.exec_module(importer)


@pytest.fixture
def fresh_pair(tmp_path):
    root = tmp_path / "runtime"
    for mode in ("TEST", "LIVE"):
        Store(root / mode / "state.sqlite", mode).close()
    profile = tmp_path / "strategy-profile.json"
    prefs = Preferences(
        minBuyPriceCents="20.5", orderAmount="3.25", stopLossEnabled=False
    ).model_dump(mode="json")
    profile.write_text(json.dumps(prefs), encoding="utf-8")
    return root, profile, prefs


def saved(root, mode):
    with sqlite3.connect(root / mode / "state.sqlite") as db:
        rows = dict(db.execute("SELECT key, value FROM meta"))
        tables = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("native_events", "intents", "tokens", "books")
        }
    return {key: json.loads(value) for key, value in rows.items()}, tables


def test_seeds_both_modes_and_changes_only_preferences(fresh_pair):
    root, profile, prefs = fresh_pair
    before = {mode: saved(root, mode) for mode in ("TEST", "LIVE")}

    importer.apply_preferences(root, profile)

    for mode in ("TEST", "LIVE"):
        meta, tables = saved(root, mode)
        prior, prior_tables = before[mode]
        assert meta.pop("preferences") == prefs
        prior.pop("preferences")
        assert meta == prior
        assert tables == prior_tables


@pytest.mark.parametrize(
    "mode,change",
    [
        ("TEST", "history"),
        ("LIVE", "intent"),
        ("TEST", "discovery"),
        ("LIVE", "running"),
        ("LIVE", "business"),
        ("TEST", "metadata"),
        ("LIVE", "edited_preferences"),
        ("TEST", "wrong_mode"),
    ],
)
def test_rejects_used_or_wrong_ledgers_without_touching_either(fresh_pair, mode, change):
    root, profile, _ = fresh_pair
    with sqlite3.connect(root / mode / "state.sqlite") as db:
        if change == "history":
            db.execute(
                "INSERT INTO native_events(event_id, kind, payload) VALUES('x','OrderFilled','{}')"
            )
        elif change == "intent":
            db.execute("INSERT INTO intents VALUES('o','e','t','BUY','{}')")
        elif change == "discovery":
            db.execute("INSERT INTO tokens VALUES('t','{}')")
        else:
            value = {
                "running": '"RUNNING"',
                "business": '{"cycles":{"used":{}}}',
                "metadata": '"scan ran"',
                "edited_preferences": json.dumps(
                    Preferences(orderAmount="2").model_dump(mode="json")
                ),
                "wrong_mode": '"TEST"' if mode == "LIVE" else '"LIVE"',
            }[change]
            key = {
                "running": "status",
                "business": "business",
                "metadata": "scan",
                "edited_preferences": "preferences",
                "wrong_mode": "mode",
            }[change]
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))
    before = {item: saved(root, item) for item in ("TEST", "LIVE")}

    with pytest.raises(ValueError):
        importer.apply_preferences(root, profile)

    assert {item: saved(root, item) for item in ("TEST", "LIVE")} == before


def test_rejects_missing_database_without_creating_it(fresh_pair):
    root, profile, _ = fresh_pair
    missing = root / "LIVE" / "state.sqlite"
    missing.unlink()
    before = saved(root, "TEST")
    with pytest.raises(FileNotFoundError):
        importer.apply_preferences(root, profile)
    assert not missing.exists()
    assert saved(root, "TEST") == before


def test_rejects_extra_field_and_duplicate_keys_without_changes(fresh_pair, capsys):
    root, profile, prefs = fresh_pair
    before = {mode: saved(root, mode) for mode in ("TEST", "LIVE")}
    profile.write_text(json.dumps(prefs | {"private_key": "SECRET_MARKER"}), encoding="utf-8")
    assert importer.main(["--data-dir", str(root), "--profile", str(profile)]) == 2
    assert "SECRET_MARKER" not in capsys.readouterr().err
    profile.write_text('{"orderAmount":"2","orderAmount":"3"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        importer.apply_preferences(root, profile)
    assert {mode: saved(root, mode) for mode in ("TEST", "LIVE")} == before


def test_existing_test_capital_rule_rejects_over_100u_without_clamping(fresh_pair):
    root, profile, _ = fresh_pair
    before = {mode: saved(root, mode) for mode in ("TEST", "LIVE")}
    profile.write_text(
        json.dumps(Preferences(orderAmount="100.01").model_dump(mode="json")),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="TEST.*100U"):
        importer.apply_preferences(root, profile)

    assert {mode: saved(root, mode) for mode in ("TEST", "LIVE")} == before


def test_rejects_running_app_lock(fresh_pair):
    root, profile, _ = fresh_pair
    with (root / "process.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="application is running"):
            importer.apply_preferences(root, profile)
