"""Apply public strategy rules to two unused, stopped TEST/LIVE databases.

Initialize both databases with Store before running this script. It deliberately
does not import Store: opening a used database through Store changes its status.
"""

import argparse
import fcntl
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from pm_nautilus.config import Preferences


_TABLES = {"meta", "native_events", "tokens", "books", "intents", "sqlite_sequence"}
_META_KEYS = {"mode", "generation", "preferences", "initial_capital", "business", "status"}
_EMPTY_BUSINESS = {
    "cursor": 0,
    "cycles": {},
    "targets": {},
    "banned": [],
    "settled": [],
    "claims": {},
    "realized": 0,
    "recovery_error": None,
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError("non-finite JSON value")


def _read_json(raw):
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def load_profile(path: Path) -> dict:
    """Reject metadata, credentials and partial or malformed strategy profiles."""
    data = _read_json(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != set(Preferences.model_fields):
        raise ValueError("strategy profile must contain exactly the Preferences fields")
    return Preferences.model_validate(data).model_dump(mode="json")


def _check_fresh(connection: sqlite3.Connection, schema: str, mode: str) -> None:
    if connection.execute(f"PRAGMA {schema}.quick_check").fetchone() != ("ok",):
        raise ValueError(f"{mode} database integrity check failed")
    objects = connection.execute(
        f"SELECT type, name FROM {schema}.sqlite_master WHERE type IN ('table', 'view', 'trigger')"
    ).fetchall()
    if {name for kind, name in objects if kind == "table"} != _TABLES or any(
        kind != "table" for kind, _ in objects
    ):
        raise ValueError(f"{mode} database schema is not fresh")
    for table in ("native_events", "intents", "tokens", "books"):
        if connection.execute(f"SELECT 1 FROM {schema}.{table} LIMIT 1").fetchone():
            raise ValueError(f"{mode} database has activity in {table}")
    meta = dict(connection.execute(f"SELECT key, value FROM {schema}.meta").fetchall())
    if set(meta) != _META_KEYS:
        raise ValueError(f"{mode} database has noninitial metadata")
    decoded = {key: _read_json(value) for key, value in meta.items()}
    if decoded["mode"] != mode or decoded["status"] != "PAUSED":
        raise ValueError(f"{mode} database has wrong mode or is not PAUSED")
    generation = decoded["generation"]
    if not isinstance(generation, str) or UUID(generation).version != 4:
        raise ValueError(f"{mode} database has invalid generation")
    if decoded["initial_capital"] != (100_000_000 if mode == "TEST" else None):
        raise ValueError(f"{mode} database has modified capital")
    if decoded["business"] != _EMPTY_BUSINESS:
        raise ValueError(f"{mode} database has business activity")
    saved = decoded["preferences"]
    if not isinstance(saved, dict) or set(saved) != set(Preferences.model_fields):
        raise ValueError(f"{mode} database has unexpected preferences")
    if Preferences.model_validate(saved).model_dump(mode="json") != Preferences().model_dump(
        mode="json"
    ):
        raise ValueError(f"{mode} database preferences were already edited")


def apply_preferences(data_dir: Path, profile_path: Path) -> None:
    """Validate both ledgers under the app lock, then update only their preferences."""
    profile = load_profile(profile_path)
    if Preferences.model_validate(profile).budget > 100_000_000:
        raise ValueError("profile orderAmount exceeds the new TEST database's 100U initial capital")
    root = data_dir.resolve(strict=True)
    test_path = (root / "TEST" / "state.sqlite").resolve(strict=True)
    live_path = (root / "LIVE" / "state.sqlite").resolve(strict=True)
    if test_path == live_path:
        raise ValueError("TEST and LIVE databases must be separate")
    lock_path = root / "process.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("application is running; stop it before importing rules") from exc
        # mode=rw refuses to create either database. One attached transaction rolls
        # back both modes on normal SQL errors; WAL cannot guarantee cross-file
        # atomicity during an abrupt crash or power failure.
        with closing(sqlite3.connect(test_path.as_uri() + "?mode=rw", uri=True)) as db:
            db.execute("PRAGMA busy_timeout=0")
            db.execute("ATTACH DATABASE ? AS live", (live_path.as_uri() + "?mode=rw",))
            try:
                db.execute("BEGIN IMMEDIATE")
                _check_fresh(db, "main", "TEST")
                _check_fresh(db, "live", "LIVE")
                encoded = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
                for schema in ("main", "live"):
                    result = db.execute(
                        f"UPDATE {schema}.meta SET value=? WHERE key='preferences'", (encoded,)
                    )
                    if result.rowcount != 1:
                        raise ValueError("preferences row changed during import")
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path, help="Stopped app data directory")
    parser.add_argument("--profile", required=True, type=Path, help="Public 15-field JSON profile")
    args = parser.parse_args(argv)
    try:
        apply_preferences(args.data_dir, args.profile)
    except (OSError, sqlite3.DatabaseError, ValueError, ValidationError) as exc:
        # Errors must never echo profile contents: someone may have put a secret in it.
        if isinstance(exc, (sqlite3.DatabaseError, ValidationError)):
            reason = type(exc).__name__
        else:
            reason = str(exc)
        print(f"规则导入失败，数据库未确认更新：{reason}", file=sys.stderr)
        return 2
    print("已将公开规则写入全新 TEST 与 LIVE 数据库；两个模式仍为 PAUSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
