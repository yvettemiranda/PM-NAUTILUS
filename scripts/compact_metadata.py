"""Losslessly compact token metadata in stopped TEST/LIVE ledgers, after backups.

This tool never opens a Runtime or Store, changes trading state, or removes rows.
The running application's process lock must be available. Backups contain private
ledger data and remain in the explicitly supplied, new private backup directory.
"""

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import ExitStack, closing
from pathlib import Path

from pm_nautilus.store import Store


def _identifier(value):
    return '"' + value.replace('"', '""') + '"'


def _integrity(db):
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("full database integrity check failed")


def logical_fingerprints(db):
    """Compare every stored value; only Token's lossless encoding may differ."""
    result = {}
    tables = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    for (name,) in tables:
        columns = db.execute(f"PRAGMA table_info({_identifier(name)})").fetchall()
        keys = [column[1] for column in sorted(columns, key=lambda row: row[5]) if column[5]]
        order = keys or [column[1] for column in columns]
        sql = f"SELECT * FROM {_identifier(name)} ORDER BY " + ",".join(
            _identifier(key) for key in order
        )
        names = [column[1] for column in columns]
        digest = hashlib.sha256()
        count = 0
        for row in db.execute(sql):
            for key, value in zip(names, row, strict=True):
                if name == "tokens" and key == "payload":
                    value = json.dumps(
                        Store.decode_token_payload(value),
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                if isinstance(value, bytes):
                    tag, raw = b"bytes", value
                else:
                    tag = type(value).__name__.encode()
                    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                digest.update(len(tag).to_bytes(8, "big") + tag)
                digest.update(len(raw).to_bytes(8, "big") + raw)
            count += 1
        result[name] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def _private_file(path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)


def _file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compact_metadata(data_dir, backup_dir, *, vacuum=False):
    root = Path(data_dir).resolve(strict=True)
    paths = {mode: (root / mode / "state.sqlite").resolve(strict=True) for mode in ("TEST", "LIVE")}
    if len(set(paths.values())) != 2 or any(
        not path.is_relative_to(root) for path in paths.values()
    ):
        raise ValueError("ledgers must be separate files inside the data directory")
    backup = Path(backup_dir).absolute()
    report = {"format": 1, "vacuum": vacuum, "status": "incomplete", "modes": {}}
    with (root / "process.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("application is running; stop it before metadata compaction") from exc
        with ExitStack() as stack:
            databases = {}
            for mode, path in paths.items():
                db = stack.enter_context(
                    closing(
                        sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None)
                    )
                )
                db.execute("PRAGMA busy_timeout=0")
                db.execute("PRAGMA synchronous=FULL")
                row = db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
                if row is None or json.loads(row[0]) != mode:
                    raise ValueError("ledger mode mismatch")
                databases[mode] = db
            # Refuse to overwrite an older backup, including through a symlink.
            backup.mkdir(mode=0o700)
            manifest = backup / "manifest.json"
            _private_file(manifest)
            try:
                # Back up both modes before changing either. These are database
                # rollback copies, not a replacement for the encrypted wallet backup.
                for mode, db in databases.items():
                    saved = backup / f"{mode}.sqlite"
                    _private_file(saved)
                    with closing(sqlite3.connect(saved)) as target:
                        db.backup(target)
                        target.execute("PRAGMA journal_mode=DELETE")
                        _integrity(target)
                    report["modes"][mode] = {
                        "backup": saved.name,
                        "backup_sha256": _file_digest(saved),
                        "file_bytes_before": paths[mode].stat().st_size,
                        "status": "backed_up",
                    }
                for mode, db in databases.items():
                    before = logical_fingerprints(db)
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        stats = Store.compact_tokens(db)
                        if logical_fingerprints(db) != before:
                            raise ValueError("ledger logical data changed during compaction")
                        _integrity(db)
                        db.execute("COMMIT")
                    except BaseException:
                        if db.in_transaction:
                            db.execute("ROLLBACK")
                        raise
                    if vacuum:
                        # SQLite rebuilds the same logical database. This is
                        # explicit because it needs extra disk space and time.
                        db.execute("VACUUM")
                        _integrity(db)
                        if logical_fingerprints(db) != before:
                            raise ValueError("ledger logical data changed during vacuum")
                    checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if checkpoint[0]:
                        raise ValueError("database checkpoint is busy")
                    report["modes"][mode].update(
                        status="verified",
                        tokens=stats,
                        logical_fingerprints=before,
                        file_bytes_after=paths[mode].stat().st_size,
                    )
                report["status"] = "verified"
            finally:
                # Retain rollback copies and the partial status even on failure.
                with manifest.open("w", encoding="utf-8") as output:
                    json.dump(report, output, ensure_ascii=False, indent=2)
                    output.flush()
                    os.fsync(output.fileno())
                directory = os.open(backup, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", required=True, type=Path, help="Stopped application's data root"
    )
    parser.add_argument(
        "--backup-dir", required=True, type=Path, help="New private rollback directory"
    )
    parser.add_argument("--vacuum", action="store_true", help="Also rebuild files to reclaim space")
    args = parser.parse_args(argv)
    try:
        report = compact_metadata(args.data_dir, args.backup_dir, vacuum=args.vacuum)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(
            f"Metadata compaction did not finish ({type(exc).__name__}); "
            "inspect the private backup manifest before restarting or retrying.",
            file=sys.stderr,
        )
        return 2
    for mode, result in report["modes"].items():
        print(
            f"{mode}: verified; tokens={result['tokens']['rows']}; "
            f"file_bytes={result['file_bytes_before']}->{result['file_bytes_after']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
