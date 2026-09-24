"""A rules export must never become a ledger or credentials export."""

import json
import sqlite3
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from pm_nautilus.config import Preferences

script = Path(__file__).resolve().parents[1] / "scripts" / "export_preferences.py"
spec = spec_from_file_location("export_preferences", script)
exporter = module_from_spec(spec)
spec.loader.exec_module(exporter)
export_preferences = exporter.export_preferences
main = exporter.main


@pytest.fixture
def running_database(tmp_path):
    path = tmp_path / "state.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("CREATE TABLE native_events(payload TEXT)")
    connection.execute("INSERT INTO meta VALUES('mode', ?)", (json.dumps("TEST"),))
    connection.execute(
        "INSERT INTO meta VALUES('preferences', ?)",
        (json.dumps(Preferences().model_dump(mode="json")),),
    )
    connection.execute("INSERT INTO meta VALUES('private_key', 'TOP_SECRET_MARKER')")
    connection.execute("INSERT INTO native_events VALUES('TRADE_HISTORY_MARKER')")
    connection.commit()
    try:
        yield path, connection
    finally:
        connection.close()


def test_reads_latest_committed_wal_rules_without_history_or_secrets(running_database):
    path, connection = running_database
    prefs = Preferences(orderAmount="3.25", stopLossEnabled=False).model_dump(mode="json")
    connection.execute("UPDATE meta SET value=? WHERE key='preferences'", (json.dumps(prefs),))
    connection.commit()

    exported = export_preferences(path, "TEST")
    parsed = json.loads(exported)
    assert set(parsed) == set(Preferences.model_fields)
    assert parsed == prefs
    assert "TOP_SECRET_MARKER" not in exported
    assert "TRADE_HISTORY_MARKER" not in exported
    assert exported == json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert connection.execute("SELECT COUNT(*) FROM native_events").fetchone()[0] == 1


def test_rejects_wrong_mode_and_unexpected_fields_without_echo(running_database, capsys):
    path, connection = running_database
    assert main(["--db", str(path), "--mode", "LIVE"]) == 2
    assert capsys.readouterr().out == ""

    prefs = Preferences().model_dump(mode="json") | {"private_key": "TOP_SECRET_MARKER"}
    connection.execute("UPDATE meta SET value=? WHERE key='preferences'", (json.dumps(prefs),))
    connection.commit()
    assert main(["--db", str(path), "--mode", "TEST"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "TOP_SECRET_MARKER" not in output.err


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "absent.sqlite"
    with pytest.raises(FileNotFoundError):
        export_preferences(path, "TEST")
    assert not path.exists()
