"""Bounded host storage housekeeping; never opens application data or stops trading.

Only explicitly registered disposable archives inside this tool's private artifact
directory may expire. Existing deployment/rollback backups are never discovered.
Under pressure, unused old project images may be removed without Docker's force
flag; the current image, pinned revisions and two newest rollback images survive.
Run without --apply to inspect the proposed changes. Status is always private.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

GIB = 1024**3
MAX_JSON = 1024 * 1024
MAX_ARTIFACT = 512 * 1024 * 1024
MAX_ENTRIES = 512
DEFAULT_ARTIFACTS = Path("/opt/pm-nautilus/backups/managed-space")
IMAGE_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
REVISION_RE = re.compile(r"[0-9a-f]{40}\Z")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.(?:log(?:\.gz)?|tgz\.age)\Z")


class MaintenanceError(Exception):
    """Safe diagnostic codes only; never include commands, paths or Docker output."""


def private_directory(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise MaintenanceError("directory_symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise MaintenanceError("directory_permissions")
    return path


def open_regular(path, *, dir_fd=None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise MaintenanceError("not_single_regular_file")
    return descriptor, info


def read_json(path, default):
    try:
        descriptor, info = open_regular(path)
    except FileNotFoundError:
        return default
    with os.fdopen(descriptor, "rb") as source:
        if info.st_size > MAX_JSON:
            raise MaintenanceError("state_too_large")
        try:
            return json.loads(source.read(MAX_JSON + 1))
        except (ValueError, UnicodeError) as exc:
            raise MaintenanceError("invalid_state") from exc


def write_json(path, value):
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(root):
    descriptor = os.open(root / "maintenance.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise MaintenanceError("unsafe_lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceError("already_running") from exc
        yield
    finally:
        os.close(descriptor)


def registry(root):
    value = read_json(root / "artifacts.json", {"format": 1, "entries": []})
    if (
        not isinstance(value, dict)
        or value.get("format") != 1
        or not isinstance(value.get("entries"), list)
        or len(value["entries"]) > MAX_ENTRIES
    ):
        raise MaintenanceError("invalid_registry")
    names = set()
    for entry in value["entries"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or not NAME_RE.fullmatch(entry["name"])
            or entry["name"] in names
            or not isinstance(entry.get("pinned"), bool)
            or any(
                type(entry.get(key)) is not int for key in ("device", "inode", "size", "mtime_ns")
            )
            or not 0 <= entry["size"] <= MAX_ARTIFACT
            or any(
                type(entry.get(key)) not in (int, float) for key in ("registered_at", "expires_at")
            )
            or not all(math.isfinite(entry[key]) for key in ("registered_at", "expires_at"))
            or entry["registered_at"] < 0
            or entry["expires_at"] < entry["registered_at"] + 86400
            or not isinstance(entry.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        ):
            raise MaintenanceError("invalid_registry_entry")
        names.add(entry["name"])
    return value


def identity(info):
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
    }


def fingerprint(path, deadline, *, dir_fd=None):
    descriptor, info = open_regular(path, dir_fd=dir_fd)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as source:
        if info.st_size > MAX_ARTIFACT:
            raise MaintenanceError("artifact_too_large")
        while chunk := source.read(1024 * 1024):
            if time.monotonic() >= deadline:
                raise MaintenanceError("time_budget")
            digest.update(chunk)
        if identity(os.fstat(source.fileno())) != identity(info):
            raise MaintenanceError("artifact_changed")
    return {**identity(info), "sha256": digest.hexdigest()}


def register(root, name, expires_days, *, artifact_dir=DEFAULT_ARTIFACTS, pinned=False, now=None):
    if not NAME_RE.fullmatch(name) or not 1 <= expires_days <= 3650:
        raise MaintenanceError("invalid_registration")
    artifacts = private_directory(artifact_dir)
    value = registry(root)
    if len(value["entries"]) >= MAX_ENTRIES or any(
        entry["name"] == name for entry in value["entries"]
    ):
        raise MaintenanceError("already_registered_or_full")
    now = time.time() if now is None else now
    entry = {
        "name": name,
        "registered_at": now,
        "expires_at": now + expires_days * 86400,
        "pinned": pinned,
        **fingerprint(artifacts / name, time.monotonic() + 30),
    }
    value["entries"].append(entry)
    write_json(root / "artifacts.json", value)
    return entry


def disk_usage(path):
    info = os.statvfs(path)
    total = info.f_blocks * info.f_frsize
    available = info.f_bavail * info.f_frsize
    if total <= 0:
        raise MaintenanceError("invalid_disk_size")
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_percent": 100 * (1 - available / total),
    }


def pressure(disk, previous=False):
    if disk["used_percent"] >= 90 or disk["available_bytes"] <= 2 * GIB:
        return "critical"
    if disk["used_percent"] >= 80 or disk["available_bytes"] <= 6 * GIB:
        return "pressure"
    if previous and (disk["used_percent"] > 70 or disk["available_bytes"] < 8 * GIB):
        return "pressure"
    return "normal"


class Docker:
    def __init__(self, deadline, executable="/usr/bin/docker"):
        self.deadline = deadline
        self.executable = executable

    def command(self, *args):
        timeout = min(10, self.deadline - time.monotonic())
        if timeout <= 0:
            raise MaintenanceError("time_budget")
        try:
            result = subprocess.run(
                [self.executable, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
                env={
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                    "DOCKER_HOST": "unix:///var/run/docker.sock",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MaintenanceError("docker_unavailable_or_timeout") from exc
        if result.returncode or len(result.stdout) > MAX_JSON:
            raise MaintenanceError("docker_command_failed")
        return result.stdout.decode("utf-8", errors="strict").strip()

    def inventory(self):
        ids = self.command("image", "ls", "--no-trunc", "--quiet", "pm-nautilus").splitlines()
        ids = sorted(set(ids))
        if len(ids) > MAX_ENTRIES or any(not IMAGE_RE.fullmatch(value) for value in ids):
            raise MaintenanceError("docker_inventory_invalid")
        images = []
        if ids:
            raw = self.command(
                "image",
                "inspect",
                "--format",
                '{"id":{{json .Id}},"created":{{json .Created}},"tags":{{json .RepoTags}}}',
                *ids,
            )
            try:
                for line in raw.splitlines():
                    item = json.loads(line)
                    if (
                        item["id"] not in ids
                        or not isinstance(item["tags"], list)
                        or not item["tags"]
                        or any(
                            not re.fullmatch(r"pm-nautilus:[0-9a-f]{40}", tag)
                            for tag in item["tags"]
                        )
                    ):
                        raise MaintenanceError("docker_inventory_ambiguous")
                    item["created_at"] = datetime.fromisoformat(
                        item["created"].replace("Z", "+00:00")
                    ).timestamp()
                    images.append(item)
            except (ValueError, TypeError, KeyError) as exc:
                raise MaintenanceError("docker_inventory_invalid") from exc
            if len(images) != len(ids) or len({item["id"] for item in images}) != len(ids):
                raise MaintenanceError("docker_inventory_incomplete")
        containers = self.command("container", "ls", "-aq", "--no-trunc").splitlines()
        if len(containers) > MAX_ENTRIES or any(
            not re.fullmatch(r"[0-9a-f]{64}", value) for value in containers
        ):
            raise MaintenanceError("docker_containers_invalid")
        used = set()
        if containers:
            used = set(
                self.command(
                    "container", "inspect", "--format", "{{.Image}}", *containers
                ).splitlines()
            )
            if not used or any(not IMAGE_RE.fullmatch(value) for value in used):
                raise MaintenanceError("docker_containers_invalid")
        return images, used

    def remove(self, image_id):
        self.command("image", "rm", image_id)


def current_revision(env_path):
    descriptor, info = open_regular(env_path)
    with os.fdopen(descriptor, "rb") as source:
        if info.st_size > 65536:
            raise MaintenanceError("environment_too_large")
        values = []
        for line in source.read(65537).decode("utf-8").splitlines():
            if line.strip().startswith("PM_GIT_REVISION="):
                values.append(line.strip().partition("=")[2].strip().strip("\"'"))
    if len(values) != 1 or not REVISION_RE.fullmatch(values[0]):
        raise MaintenanceError("current_revision_unknown")
    return values[0]


def image_candidates(docker, env_path, pinned, now):
    revision = current_revision(env_path)
    images, used = docker.inventory()
    tags = {tag: image["id"] for image in images for tag in image["tags"]}
    current = tags.get("pm-nautilus:" + revision)
    if not current or any("pm-nautilus:" + value not in tags for value in pinned):
        raise MaintenanceError("protected_image_missing")
    protected = {current, *used, *(tags["pm-nautilus:" + value] for value in pinned)}
    rollback = sorted(
        (item for item in images if item["id"] != current),
        key=lambda item: (item["created_at"], item["id"]),
        reverse=True,
    )
    protected.update(item["id"] for item in rollback[:2])
    return sorted(
        (
            item
            for item in images
            if item["id"] not in protected and now - item["created_at"] >= 14 * 86400
        ),
        key=lambda item: (item["created_at"], item["id"]),
    )


def maintain(
    root,
    env_path,
    disk_path,
    *,
    artifact_dir=DEFAULT_ARTIFACTS,
    apply=False,
    pinned=(),
    max_removals=2,
    budget=45,
    docker=None,
    now=None,
):
    if (
        not 1 <= max_removals <= 5
        or not 5 <= budget <= 50
        or any(not REVISION_RE.fullmatch(value) for value in pinned)
    ):
        raise MaintenanceError("invalid_limits")
    deadline = time.monotonic() + budget
    now = time.time() if now is None else now
    previous = read_json(root / "status.json", {})
    disk = disk_usage(disk_path)
    if not isinstance(previous, dict):
        raise MaintenanceError("invalid_status")
    level = pressure(disk, previous.get("pressure_latched", False))
    report = {
        "format": 1,
        "checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "apply": apply,
        "ok": True,
        "level": level,
        "pressure_latched": level != "normal",
        "disk": disk,
        "actions": [],
        "errors": [],
        "limits": {"removals": max_removals, "seconds": budget},
        "eligible_artifacts": 0,
        "eligible_images": None,
    }
    removed = 0
    try:
        artifacts = private_directory(artifact_dir)
        value = registry(root)
        newest = sorted(value["entries"], key=lambda item: item["registered_at"], reverse=True)[:2]
        protected = {item["name"] for item in newest}
        report["eligible_artifacts"] = sum(
            not entry["pinned"] and entry["name"] not in protected and entry["expires_at"] <= now
            for entry in value["entries"]
        )
        for entry in sorted(value["entries"], key=lambda item: item["expires_at"]):
            if removed >= max_removals or time.monotonic() >= deadline:
                break
            if entry["pinned"] or entry["name"] in protected or entry["expires_at"] > now:
                continue
            try:
                artifact_fd = os.open(artifacts, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    folder_info = os.fstat(artifact_fd)
                    if folder_info.st_uid != os.geteuid() or folder_info.st_mode & 0o077:
                        raise MaintenanceError("directory_permissions")
                    actual = fingerprint(entry["name"], deadline, dir_fd=artifact_fd)
                    if any(actual[key] != entry[key] for key in actual):
                        raise MaintenanceError("registered_artifact_changed")
                    # Recheck and unlink relative to the same open private directory.
                    path_info = os.stat(entry["name"], dir_fd=artifact_fd, follow_symlinks=False)
                    if (
                        not stat.S_ISREG(path_info.st_mode)
                        or path_info.st_nlink != 1
                        or identity(path_info) != {key: entry[key] for key in identity(path_info)}
                    ):
                        raise MaintenanceError("registered_artifact_changed")
                    if apply:
                        os.unlink(entry["name"], dir_fd=artifact_fd)
                    report["actions"].append(
                        {
                            "kind": "expired_artifact",
                            "name": entry["name"],
                            "bytes": entry["size"],
                            "removed": apply,
                        }
                    )
                    if apply:
                        value["entries"].remove(entry)
                        write_json(root / "artifacts.json", value)
                finally:
                    os.close(artifact_fd)
                removed += 1
            except (OSError, MaintenanceError) as exc:
                report["errors"].append(
                    exc.args[0] if isinstance(exc, MaintenanceError) else "artifact_io_error"
                )
        if level != "normal" and removed < max_removals:
            docker = docker or Docker(deadline)
            candidates = image_candidates(docker, env_path, pinned, now)
            report["eligible_images"] = len(candidates)
            for item in candidates:
                if removed >= max_removals or time.monotonic() >= deadline:
                    break
                if apply:
                    # A deployment or container change since inventory protects its
                    # images before deletion. Docker independently refuses in-use IDs.
                    latest = image_candidates(docker, env_path, pinned, now)
                    if item["id"] not in {image["id"] for image in latest}:
                        continue
                    docker.remove(item["id"])
                report["actions"].append(
                    {"kind": "unused_project_image", "id": item["id"], "removed": apply}
                )
                removed += 1
                if apply and pressure(disk_usage(disk_path), True) == "normal":
                    break
        report["disk_after"] = disk_usage(disk_path)
        report["level"] = pressure(report["disk_after"], level != "normal")
        report["pressure_latched"] = report["level"] != "normal"
    except (OSError, ValueError, MaintenanceError) as exc:
        report["errors"].append(
            exc.args[0] if isinstance(exc, MaintenanceError) else "maintenance_io_error"
        )
    if time.monotonic() >= deadline:
        report["errors"].append("time_budget")
    report["reserve_recovered"] = report["level"] == "normal"
    report["operator_action_required"] = report["level"] == "critical" or bool(report["errors"])
    report["safe_reclaim_available"] = bool(
        report["eligible_artifacts"] or report["eligible_images"]
    )
    report["ok"] = not report["errors"]
    report["status"] = "degraded" if report["errors"] else report["level"]
    write_json(root / "status.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/pm-nautilus-maintenance"))
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACTS)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--apply", action="store_true")
    run.add_argument("--env-file", type=Path, default=Path("/opt/pm-nautilus/.env"))
    run.add_argument("--disk-path", type=Path, default=Path("/opt/pm-nautilus"))
    run.add_argument("--protect-revision", action="append", default=[])
    run.add_argument("--max-removals", type=int, default=2)
    run.add_argument("--budget-seconds", type=int, default=45)
    add = sub.add_parser("register")
    add.add_argument("name", help="Existing regular archive in the private artifact directory only")
    add.add_argument("--expires-days", required=True, type=int)
    add.add_argument("--pinned", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        root = private_directory(args.state_dir)
        with locked(root):
            if args.command == "register":
                register(
                    root,
                    args.name,
                    args.expires_days,
                    artifact_dir=args.artifact_dir,
                    pinned=args.pinned,
                )
                print(
                    "PM-NAUTILUS disposable archive registered; newest two and pinned archives retained."
                )
                return 0
            report = maintain(
                root,
                args.env_file,
                args.disk_path,
                artifact_dir=args.artifact_dir,
                apply=args.apply,
                pinned=args.protect_revision,
                max_removals=args.max_removals,
                budget=args.budget_seconds,
            )
            print(
                f"PM-NAUTILUS storage: {report['status']}; apply={args.apply}; actions={len(report['actions'])}; errors={len(report['errors'])}"
            )
            return 0 if report["ok"] and report["level"] != "critical" else 2
    except (OSError, ValueError, MaintenanceError) as exc:
        code = exc.args[0] if isinstance(exc, MaintenanceError) else "maintenance_failed"
        print(f"PM-NAUTILUS storage: {code}; no unregistered data is eligible.", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
