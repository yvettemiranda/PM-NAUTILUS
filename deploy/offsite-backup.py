#!/usr/bin/env python3
"""Daily off-server backup of the PM-NAUTILUS data directory to Tencent COS.

Runs on the host with only the Python standard library and the official
``coscli`` binary. It never stops the application:

* every ``*.sqlite`` ledger is copied with SQLite's online backup API from a
  read-only connection, which yields a transactionally consistent copy while
  the app keeps writing (WAL readers do not block the writer), then verified
  with ``PRAGMA quick_check``;
* other durable files (execution-mode.json, the encrypted wallet vault, ...)
  are copied as-is; WAL/SHM side files, the process lock and every ``.pm-*``
  scratch file or directory (wallet export/restore/upload work areas, at any
  depth) are skipped;
* a manifest with SHA-256 checksums and the deployed revision is added, the
  archive is uploaded, and only the newest local archives are kept.

The archive itself is not encrypted: the wallet vault inside stays encrypted by
its own password, but the ledgers are plain. Protect the COS bucket accordingly.

Run it as the application's UID (10001) so nothing it touches in the data
directory can become root-owned. See deploy/pm-nautilus-offsite-backup.*.example.

Configuration (environment, e.g. EnvironmentFile=/etc/pm-nautilus/offsite.env):
  PM_OFFSITE_COS_URI   cos://<bucket-appid>/<prefix>/   (required unless --no-upload)
  PM_OFFSITE_COSCLI    path to coscli            (default: coscli)
  PM_OFFSITE_COSCLI_CONFIG  coscli config file   (default: coscli's own default)
  PM_APP_DIR           application directory     (default: /opt/pm-nautilus)
  PM_OFFSITE_STAGING   local staging directory   (default: /var/lib/pm-nautilus-offsite)
  PM_OFFSITE_KEEP_LOCAL  archives kept locally   (default: 2)
"""

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

SKIP_SUFFIXES = ("-wal", "-shm", "-journal")
SKIP_NAMES = {"process.lock"}
SKIP_PREFIXES = (".pm-",)  # live export/restore/upload/final scratch files and dirs


def log(message):
    print(f"[offsite-backup] {message}", flush=True)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def skipped(relative: Path) -> bool:
    """Skip side files and anything inside a ``.pm-*`` scratch file or directory."""
    name = relative.name
    return (
        name in SKIP_NAMES
        or name.endswith(SKIP_SUFFIXES)
        or any(part.startswith(SKIP_PREFIXES) for part in relative.parts)
    )


def copy_sqlite(source: Path, target: Path):
    """Consistent online copy; never creates files next to the live ledger."""
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=60)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            result = dst.execute("PRAGMA quick_check").fetchone()[0]
            # The copy is a standalone file: keep it in rollback-journal mode so
            # it opens cleanly anywhere without -wal/-shm companions.
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    finally:
        src.close()
    if result != "ok":
        raise RuntimeError(f"{source}: quick_check failed: {result}")


def require_ledger_owner(data_dir: Path):
    """Opening a WAL ledger can create -wal/-shm files beside it, owned by us.

    Root-owned side files would stop the application (UID 10001) from opening
    its own ledger, so only the ledger owner may run this backup.
    """
    owners = {
        path.stat().st_uid
        for path in data_dir.rglob("*.sqlite")
        if path.is_file() and not skipped(path.relative_to(data_dir))
    }
    if owners and os.geteuid() not in owners:
        raise RuntimeError(
            f"run as the ledger owner UID {sorted(owners)} (systemd User=10001), "
            f"not UID {os.geteuid()}: foreign-owned SQLite side files would block the app"
        )


def snapshot(data_dir: Path, work: Path) -> list[dict]:
    files = []
    for path in sorted(data_dir.rglob("*")):
        relative = path.relative_to(data_dir)
        if not path.is_file() or path.is_symlink() or skipped(relative):
            continue
        target = work / "data" / relative
        if path.suffix == ".sqlite":
            copy_sqlite(path, target)
            kind = "sqlite-online-backup"
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            kind = "file"
        files.append(
            {
                "path": relative.as_posix(),
                "kind": kind,
                "bytes": target.stat().st_size,
                "sha256": sha256(target),
            }
        )
    if not any(item["kind"] == "sqlite-online-backup" for item in files):
        raise RuntimeError(f"{data_dir} contains no ledgers; refusing an empty backup")
    return files


def revision(app_dir: Path) -> str | None:
    try:
        for line in (app_dir / ".env").read_text().splitlines():
            if line.startswith("PM_GIT_REVISION="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def build_archive(app_dir: Path, staging: Path) -> Path:
    data_dir = app_dir / "runtime" / "server"
    if not data_dir.is_dir():
        raise RuntimeError(f"data directory not found: {data_dir}")
    require_ledger_owner(data_dir)
    staging.mkdir(parents=True, exist_ok=True)
    os.chmod(staging, 0o700)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = f"pm-nautilus-{socket.gethostname()}-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory(dir=staging, prefix=".work-") as tmp:
        work = Path(tmp)
        files = snapshot(data_dir, work)
        manifest = {
            "createdAt": stamp,
            "host": socket.gethostname(),
            "revision": revision(app_dir),
            "dataDir": str(data_dir),
            "files": files,
        }
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        partial = staging / f".{name}.partial"
        old_umask = os.umask(0o077)
        try:
            with tarfile.open(partial, "w:gz") as archive:
                archive.add(work / "manifest.json", arcname="manifest.json")
                archive.add(work / "data", arcname="data")
        finally:
            os.umask(old_umask)
        final = staging / name
        os.replace(partial, final)
    total = sum(item["bytes"] for item in files)
    log(f"archive {final.name}: {len(files)} files, {total} bytes before compression")
    return final


def upload(archive: Path, uri: str, coscli: str, config: str | None):
    if not uri.startswith("cos://"):
        raise RuntimeError("PM_OFFSITE_COS_URI must look like cos://<bucket-appid>/<prefix>/")
    destination = uri.rstrip("/") + "/" + archive.name
    command = [coscli, "cp", str(archive), destination]
    if config:
        command += ["-c", config]
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL)
    log(f"uploaded to {destination}")


def prune(staging: Path, keep: int):
    archives = sorted(staging.glob("pm-nautilus-*.tar.gz"), reverse=True)
    for old in archives[keep:]:
        old.unlink()
        log(f"removed local archive {old.name}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-upload", action="store_true", help="only build the local archive")
    args = parser.parse_args(argv)
    app_dir = Path(os.environ.get("PM_APP_DIR", "/opt/pm-nautilus"))
    staging = Path(os.environ.get("PM_OFFSITE_STAGING", "/var/lib/pm-nautilus-offsite"))
    keep = int(os.environ.get("PM_OFFSITE_KEEP_LOCAL", "2"))
    uri = os.environ.get("PM_OFFSITE_COS_URI", "")
    if not args.no_upload and not uri:
        parser.error("PM_OFFSITE_COS_URI is not set (or pass --no-upload)")
    try:
        archive = build_archive(app_dir, staging)
        if not args.no_upload:
            upload(
                archive,
                uri,
                os.environ.get("PM_OFFSITE_COSCLI", "coscli"),
                os.environ.get("PM_OFFSITE_COSCLI_CONFIG") or None,
            )
    finally:
        # Bounded on every path: repeated upload failures must not fill the disk.
        if staging.is_dir():
            prune(staging, max(1, keep))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        log(f"FAILED: {type(exc).__name__}: {exc}")
        sys.exit(1)
