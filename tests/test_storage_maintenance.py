"""Online housekeeping is bounded and cannot discover ledgers or manual backups."""

import json
import os
import subprocess
import time
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

script = Path(__file__).resolve().parents[1] / "scripts" / "storage_maintenance.py"
spec = spec_from_file_location("storage_maintenance", script)
maintenance = module_from_spec(spec)
spec.loader.exec_module(maintenance)
NOW = 1_900_000_000


def disk(used=50, available=20):
    return {
        "total_bytes": 100 * maintenance.GIB,
        "available_bytes": available * maintenance.GIB,
        "used_percent": used,
    }


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = maintenance.private_directory(tmp_path / "private-status")
    artifacts = maintenance.private_directory(tmp_path / "managed-space")
    env = tmp_path / ".env"
    env.write_text("PM_GIT_REVISION=" + f"{6:040x}" + "\nPM_UI_PASSWORD=never-show-me\n")
    monkeypatch.setattr(maintenance, "disk_usage", lambda _: disk())
    return root, artifacts, env


def run(workspace, **kwargs):
    root, artifacts, env = workspace
    return maintenance.maintain(root, env, env.parent, artifact_dir=artifacts, now=NOW, **kwargs)


def archive(workspace, index, *, pinned=False):
    root, artifacts, _ = workspace
    name = f"archive-{index}.tgz.age"
    (artifacts / name).write_bytes(f"private archive {index}".encode())
    maintenance.register(
        root, name, 1, artifact_dir=artifacts, pinned=pinned, now=NOW - 10 * 86400 + index
    )
    return artifacts / name


class Images:
    def __init__(self, used=()):
        self.images = [
            {
                "id": f"sha256:{i:064x}",
                "tags": [f"pm-nautilus:{i:040x}"],
                "created_at": NOW - (100 - i) * 86400,
            }
            for i in range(1, 7)
        ]
        self.used = set(used)
        self.removed = []

    def inventory(self):
        return self.images, self.used

    def remove(self, image_id):
        self.removed.append(image_id)
        self.images = [item for item in self.images if item["id"] != image_id]


def test_dry_run_only_plans_and_apply_protects_newest_two_pinned_unregistered(workspace):
    files = [archive(workspace, i, pinned=i == 2) for i in range(5)]
    untouched = workspace[1] / "manual.tgz.age"
    untouched.write_bytes(b"not registered")
    report = run(workspace)
    assert [item["name"] for item in report["actions"]] == [files[0].name, files[1].name]
    assert all(path.exists() for path in files)
    report = run(workspace, apply=True)
    assert report["ok"] and report["level"] == "normal"
    assert not files[0].exists() and not files[1].exists()
    assert all(path.exists() for path in files[2:]) and untouched.exists()
    assert workspace[0].joinpath("status.json").stat().st_mode & 0o777 == 0o600
    assert workspace[0].joinpath("artifacts.json").stat().st_mode & 0o777 == 0o600
    assert "private archive" not in workspace[0].joinpath("status.json").read_text()


@pytest.mark.parametrize(
    "name",
    ["../manual.tgz.age", "state.sqlite", "wallet.age", ".env", "status.json", "dir/file.log"],
)
def test_registration_rejects_paths_and_protected_data_names(workspace, name):
    with pytest.raises(maintenance.MaintenanceError, match="invalid_registration"):
        maintenance.register(workspace[0], name, 1, artifact_dir=workspace[1])


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_registration_rejects_links_to_manual_backup(workspace, link):
    target = workspace[1].parent / "manual-rollback.tgz.age"
    target.write_text("keep")
    managed = workspace[1] / "linked.tgz.age"
    if link == "symlink":
        managed.symlink_to(target)
    else:
        os.link(target, managed)
    with pytest.raises((OSError, maintenance.MaintenanceError)):
        maintenance.register(workspace[0], managed.name, 1, artifact_dir=workspace[1])
    assert target.read_text() == "keep"


def test_changed_or_replaced_registered_artifact_is_retained_and_degraded(workspace):
    changed = archive(workspace, 0)
    archive(workspace, 1)
    archive(workspace, 2)
    changed.write_text("changed privately")
    report = run(workspace, apply=True)
    assert report["status"] == "degraded" and report["operator_action_required"]
    assert report["errors"] == ["registered_artifact_changed"]
    assert changed.exists()


def test_replaced_symlink_never_unlinks_target(workspace):
    managed = archive(workspace, 0)
    archive(workspace, 1)
    archive(workspace, 2)
    target = workspace[1].parent / "LIVE.sqlite"
    target.write_bytes(b"ledger")
    managed.unlink()
    managed.symlink_to(target)
    report = run(workspace, apply=True)
    assert not report["ok"] and managed.is_symlink()
    assert target.read_bytes() == b"ledger"


def test_private_directory_rejects_symlink_ancestor_and_public_mode(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(maintenance.MaintenanceError, match="directory_symlink"):
        maintenance.private_directory(alias / "child")
    real.chmod(0o755)
    with pytest.raises(maintenance.MaintenanceError, match="directory_permissions"):
        maintenance.private_directory(real)


def test_second_run_cannot_overlap(workspace):
    with maintenance.locked(workspace[0]):
        with pytest.raises(maintenance.MaintenanceError, match="already_running"):
            with maintenance.locked(workspace[0]):
                pytest.fail("overlap")


def test_pressure_hysteresis_and_reserved_bytes():
    assert maintenance.pressure(disk(80, 20)) == "pressure"
    assert maintenance.pressure(disk(50, 6)) == "pressure"
    assert maintenance.pressure(disk(90, 20)) == "critical"
    assert maintenance.pressure(disk(50, 2)) == "critical"
    assert maintenance.pressure(disk(75, 10), True) == "pressure"
    assert maintenance.pressure(disk(65, 7), True) == "pressure"
    assert maintenance.pressure(disk(70, 8), True) == "normal"


def test_project_image_cleanup_protects_current_rollback_pinned_and_every_container(
    workspace, monkeypatch
):
    images = Images(used={f"sha256:{1:064x}"})
    monkeypatch.setattr(maintenance, "disk_usage", lambda _: disk(85, 5))
    report = run(workspace, apply=True, docker=images, pinned=[f"{2:040x}"])
    assert images.removed == [f"sha256:{3:064x}"]
    assert report["ok"] and report["level"] == "pressure"
    assert report["eligible_images"] == 1


def test_normal_disk_does_not_contact_docker(workspace):
    class NoDocker:
        def inventory(self):
            pytest.fail("healthy disk must not contact Docker")

    assert run(workspace, docker=NoDocker())["ok"]


def test_image_cleanup_rechecks_new_current_revision_before_removal(workspace, monkeypatch):
    images = Images(used={f"sha256:{1:064x}", f"sha256:{2:064x}"})
    revisions = iter([f"{6:040x}", f"{3:040x}"])
    monkeypatch.setattr(maintenance, "current_revision", lambda _: next(revisions))
    monkeypatch.setattr(maintenance, "disk_usage", lambda _: disk(85, 5))
    report = run(workspace, apply=True, docker=images)
    assert report["ok"] and images.removed == []


def test_maximum_two_removals_and_no_global_prune(workspace, monkeypatch):
    images = Images()
    monkeypatch.setattr(maintenance, "disk_usage", lambda _: disk(85, 5))
    report = run(workspace, apply=True, docker=images)
    assert len(report["actions"]) == len(images.removed) == 2
    assert all(action["kind"] == "unused_project_image" for action in report["actions"])


def test_docker_failure_is_observable_and_sanitized(workspace, monkeypatch):
    class Unavailable:
        def inventory(self):
            raise maintenance.MaintenanceError("docker_unavailable_or_timeout")

    monkeypatch.setattr(maintenance, "disk_usage", lambda _: disk(95, 1))
    report = run(workspace, apply=True, docker=Unavailable())
    assert report["status"] == "degraded" and report["operator_action_required"]
    assert "never-show-me" not in json.dumps(report)
    assert not report["actions"]


def test_no_safe_image_candidate_reports_critical_without_touching_manual_backups(
    workspace, monkeypatch
):
    images = Images(used={f"sha256:{i:064x}" for i in range(1, 7)})
    manual = workspace[1].parent / "pre-metadataopt-rollback"
    manual.mkdir()
    (manual / "TEST.sqlite").write_text("ledger")
    monkeypatch.setattr(maintenance, "disk_usage", lambda _: disk(95, 1))
    report = run(workspace, apply=True, docker=images)
    assert report["level"] == "critical" and report["operator_action_required"]
    assert not report["safe_reclaim_available"] and not report["actions"]
    assert (manual / "TEST.sqlite").read_text() == "ledger"


def test_missing_pinned_image_disables_image_cleanup(workspace, monkeypatch):
    images = Images()
    monkeypatch.setattr(maintenance, "disk_usage", lambda _: disk(85, 5))
    report = run(workspace, apply=True, docker=images, pinned=["f" * 40])
    assert report["errors"] == ["protected_image_missing"] and not images.removed


def test_docker_parser_refuses_other_project_alias(workspace, monkeypatch):
    image_id = "sha256:" + "a" * 64
    outputs = iter(
        [
            image_id,
            json.dumps(
                {
                    "id": image_id,
                    "created": "2026-01-01T00:00:00Z",
                    "tags": ["pm-nautilus:" + "a" * 40, "other-project:latest"],
                }
            ),
        ]
    )
    docker = maintenance.Docker(float("inf"))
    monkeypatch.setattr(docker, "command", lambda *args: next(outputs))
    with pytest.raises(maintenance.MaintenanceError, match="docker_inventory_ambiguous"):
        docker.inventory()


def test_corrupt_registry_nan_and_traversal_are_rejected_without_unlink(workspace):
    path = archive(workspace, 0)
    registry = workspace[0] / "artifacts.json"
    value = json.loads(registry.read_text())
    value["entries"][0]["expires_at"] = float("nan")
    registry.write_text(json.dumps(value))
    report = run(workspace, apply=True)
    assert report["errors"] == ["invalid_registry_entry"] and path.exists()


def test_cli_defaults_to_dry_run_and_does_not_emit_env_secrets(workspace, capsys):
    archive(workspace, 0)
    archive(workspace, 1)
    archive(workspace, 2)
    root, artifacts, env = workspace
    result = maintenance.main(
        [
            "--state-dir",
            str(root),
            "--artifact-dir",
            str(artifacts),
            "run",
            "--env-file",
            str(env),
            "--disk-path",
            str(root),
        ]
    )
    output = capsys.readouterr()
    assert result == 0 and "apply=False" in output.out
    assert "never-show-me" not in output.out + output.err
    assert len(list(artifacts.iterdir())) == 3


def test_registration_refuses_large_archive_without_reading_it(workspace):
    path = workspace[1] / "too-large.tgz.age"
    with path.open("wb") as output:
        output.truncate(maintenance.MAX_ARTIFACT + 1)
    with pytest.raises(maintenance.MaintenanceError, match="artifact_too_large"):
        maintenance.register(workspace[0], path.name, 1, artifact_dir=workspace[1])


def test_not_expired_archive_and_young_image_are_retained(workspace, monkeypatch):
    path = archive(workspace, 0)
    archive(workspace, 1)
    archive(workspace, 2)
    registry = workspace[0] / "artifacts.json"
    value = json.loads(registry.read_text())
    value["entries"][0]["expires_at"] = NOW + 100
    registry.write_text(json.dumps(value))
    images = Images(used={f"sha256:{i:064x}" for i in (1, 2, 4)})
    images.images[2]["created_at"] = NOW - 86400
    monkeypatch.setattr(maintenance, "disk_usage", lambda _: disk(85, 5))
    report = run(workspace, apply=True, docker=images)
    assert report["ok"] and path.exists() and not images.removed


def test_time_budget_stops_artifact_io_without_delete(workspace, monkeypatch):
    path = archive(workspace, 0)
    archive(workspace, 1)
    archive(workspace, 2)

    def expired(*args, **kwargs):
        raise maintenance.MaintenanceError("time_budget")

    monkeypatch.setattr(maintenance, "fingerprint", expired)
    report = run(workspace, apply=True)
    assert report["errors"] == ["time_budget"] and path.exists()


def test_docker_command_has_timeout_and_removal_cannot_force_or_prune(monkeypatch):
    calls = []

    def execute(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(maintenance.subprocess, "run", execute)
    docker = maintenance.Docker(time.monotonic() + 45)
    image_id = "sha256:" + "f" * 64
    docker.remove(image_id)
    args, options = calls[0]
    assert args == ["/usr/bin/docker", "image", "rm", image_id]
    assert 0 < options["timeout"] <= 10
    assert options["env"]["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert options["stderr"] == subprocess.DEVNULL


def test_docker_timeout_does_not_expose_subprocess_text(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("private secret command", 1, output=b"secret")

    monkeypatch.setattr(maintenance.subprocess, "run", timeout)
    docker = maintenance.Docker(time.monotonic() + 45)
    with pytest.raises(maintenance.MaintenanceError, match="^docker_unavailable_or_timeout$"):
        docker.inventory()
