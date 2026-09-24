"""Read one mode's saved strategy rules without exporting its ledger or credentials."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from pm_nautilus.config import Preferences


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
    return json.loads(
        raw,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def export_preferences(db_path: Path, mode: str) -> str:
    """Return canonical JSON containing exactly the validated Preferences fields."""
    if mode not in {"TEST", "LIVE"}:
        raise ValueError("invalid mode")

    # mode=ro observes committed WAL changes in a running server. immutable=1 would
    # ignore the WAL, and opening a normal SQLite connection could create a database.
    uri = db_path.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        rows = dict(
            connection.execute(
                "SELECT key, value FROM meta WHERE key IN ('mode', 'preferences')"
            ).fetchall()
        )

    if set(rows) != {"mode", "preferences"} or _read_json(rows["mode"]) != mode:
        raise ValueError("missing preferences or mismatched mode")
    saved = _read_json(rows["preferences"])
    if not isinstance(saved, dict) or set(saved) != set(Preferences.model_fields):
        raise ValueError("preferences fields differ from the current schema")
    validated = Preferences(**saved).model_dump(mode="json")
    return json.dumps(validated, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Existing mode state.sqlite")
    parser.add_argument("--mode", required=True, choices=("TEST", "LIVE"))
    args = parser.parse_args(argv)
    try:
        output = export_preferences(args.db, args.mode)
    except (OSError, sqlite3.DatabaseError, ValueError, ValidationError):
        # Do not echo a malformed database value: it may contain credentials.
        print("无法读取或验证该模式的规则；未导出任何内容", file=sys.stderr)
        return 2
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
