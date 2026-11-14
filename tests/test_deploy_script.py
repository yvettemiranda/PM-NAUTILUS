"""deploy/deploy.sh against stub docker/git/curl: order, backups, restore, refusals."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# The script targets the Linux server (GNU find/sed/chown); BSD tools differ.
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="deploy.sh targets Linux")

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "deploy.sh"
OLD, NEW = "a" * 40, "c" * 40
PASSWORD = 'pa"ss\\word-123456789'

FAKE = r"""
import json, os, sys
STATE = os.environ["SIM_STATE"]
s = json.load(open(STATE))
tool, args = sys.argv[1], sys.argv[2:]
s["log"].append([tool] + args)
json.dump(s, open(STATE, "w"))
def save():
    json.dump(s, open(STATE, "w"))
if tool == "id":
    print(0); sys.exit(0)
if tool == "docker":
    if "run" in args:
        sys.exit(1 if s.get("archive_check_fail") else 0)
    if "stop" in args:
        s["up"] = False
        # A graceful SQLite shutdown checkpoints and deletes its WAL files.
        for wal in __import__("pathlib").Path("runtime/server").rglob("*-wal"):
            wal.unlink()
    if "up" in args:
        env = dict(l.strip().split("=", 1) for l in open(".env") if "=" in l)
        s["up"], s["rev"] = True, env["PM_GIT_REVISION"]
        n = 1 if s.get("narrow") and s["rev"] == "c" * 40 else 4
        if s.get("rollback") and s["rev"] == "a" * 40:
            n = 10
        s["status"] = {str(k): "PAUSED" for k in range(1, n + 1)}
    save(); sys.exit(0)
if tool == "git":
    if "merge-base" in args and s.get("non_main"):
        sys.exit(1)
    if "status" in args:
        print(s.get("dirty", ""), end="")
    if "checkout" in args:
        s["head"] = args[-1]; save()
    if "rev-parse" in args:
        print(s.get("head", "b" * 40))
    if "show" in args and args[-1].endswith(":config/test-account-count"):
        if s.get("narrow") and args[-1].startswith("c" * 40):
            print(1)
        else:
            sys.exit(1)
    if "show" in args and args[-1].endswith(":src/pm_nautilus/test_accounts.py"):
        if s.get("rollback"):
            print("    count = 10")
        else:
            sys.exit(1)
    sys.exit(0)
if "-K" in args:
    s["auth"] = sys.stdin.read(); save()
url = [a for a in args if a.startswith("http")][0]
if url.startswith("https://api.github.com/"):
    assert "/commits/" + s.get("head", "x") not in url  # Queried before checkout.
    if s.get("ci") == "offline":
        sys.exit(6)
    runs = {
        "pass": [("verify (ubuntu-24.04)", "success"), ("verify (ubuntu-24.04-arm)", "success")],
        "fail": [("verify (ubuntu-24.04)", "success"), ("verify (ubuntu-24.04-arm)", "failure")],
        "pending": [("verify (ubuntu-24.04)", None), ("verify (ubuntu-24.04-arm)", "success")],
        "one": [("verify (ubuntu-24.04)", "success")],
    }[s.get("ci", "pass")]
    print(json.dumps({"check_runs": [
        {"name": n, "status": "completed" if c else "in_progress", "conclusion": c}
        for n, c in runs
    ]}))
    sys.exit(0)
if not s["up"]:
    sys.exit(7)
if "-D" in args:
    print("HTTP/1.1 200 OK\r\nx-pm-csrf: tok\r\n\r"); sys.exit(0)
if url.endswith("/api/health"):
    built = any(e[0] == "docker" and "build" in e for e in s["log"])
    if s.get("switch_after_accounts"):
        # Flip once the pre-stop account queries have run (after the build).
        queried = [e for e in s["log"] if e[0] == "curl" and any("account_id=4" in a for a in e)]
        built = len(queried) >= 2
    live = s["live"] or (built and s.get("live_after_build", False))
    wallet = s.get("wallet_after_build") if built and s.get("wallet_after_build") else s["wallet"]
    print(json.dumps({
        "status": "ok", "revision": s["rev"], "testAccountCount": len(s["status"]),
        "availableTestAccountCount": len(s["status"]), "liveExecutionEnabled": live,
        "liveWalletStatus": wallet,
        "activeExecutionMode": "TEST", "backgroundErrors": {},
        "marketData": {"TEST": {"status": "ok", "readyGroupCount": 5, "groupCount": 5,
                                "readyBookCount": 9, "tokenCount": 9}},
    }))
    sys.exit(0)
account = url.split("account_id=")[1]
if "/start" in url:
    assert "x-pm-csrf: tok" in args
    s["status"][account] = "RUNNING"; save()
print(json.dumps({"strategy": {"status": s["status"][account]},
                  "positionCount": s.get("positions", {"1": 26, "3": 46}).get(account, 0),
                  "error": None}))
"""


@pytest.fixture
def server(tmp_path):
    bin_dir, app = tmp_path / "bin", tmp_path / "app"
    bin_dir.mkdir()
    (app / "runtime" / "server").mkdir(parents=True)
    (app / "runtime" / "server" / "state.sqlite").write_bytes(os.urandom(4096))
    # Larger than the ledger: the data directory shrinks a lot when it is stopped.
    (app / "runtime" / "server" / "state.sqlite-wal").write_bytes(os.urandom(1 << 20))
    (app / "compose.yaml").write_text("services: {}\n")
    env = app / ".env"
    env.write_text(
        f"PM_UI_PASSWORD={PASSWORD}\nPM_ALLOWED_HOSTS=localhost\nPM_GIT_REVISION={OLD}\n"
    )
    env.chmod(0o600)
    (bin_dir / "fake.py").write_text(FAKE)
    for tool in ("docker", "git", "curl", "id"):
        stub = bin_dir / tool
        stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake.py"}" {tool} "$@"\n')
        stub.chmod(0o755)
    state = tmp_path / "state.json"
    status = {"1": "RUNNING", "2": "PAUSED", "3": "RUNNING", "4": "PAUSED"}
    state.write_text(
        json.dumps(
            {"up": True, "rev": OLD, "live": False, "wallet": "LOCKED", "status": status, "log": []}
        )
    )

    def run(*args, live=False, **sim):
        data = json.loads(state.read_text())
        data["live"] = live
        data.update(sim)
        state.write_text(json.dumps(data))
        environ = os.environ | {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SIM_STATE": str(state),
            "PM_APP_DIR": str(app),
            "PM_BACKUP_ROOT": str(tmp_path / "backups"),
            "PM_HEALTH_TIMEOUT": "6",
            "PM_MARKET_TIMEOUT": "6",
            "PM_DEPLOY_LOCK": str(tmp_path / "deploy.lock"),
        }
        result = subprocess.run(
            ["bash", str(SCRIPT), *args], env=environ, capture_output=True, text=True, timeout=60
        )
        return result, json.loads(state.read_text())

    return app, tmp_path / "backups", run


def test_deploy_backs_up_switches_and_restores_only_running_accounts(server):
    app, backups, run = server
    result, state = run(NEW, "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    docker = [
        entry[-1] if entry[-1] != "-d" else "up" for entry in state["log"] if entry[0] == "docker"
    ]
    assert docker == ["build", "stop", "up"]
    assert state["rev"] == NEW and f"PM_GIT_REVISION={NEW}" in (app / ".env").read_text()
    assert oct((app / ".env").stat().st_mode & 0o777) == "0o600"
    assert state["status"] == {"1": "RUNNING", "2": "PAUSED", "3": "RUNNING", "4": "PAUSED"}
    starts = [e for e in state["log"] if e[0] == "curl" and any("/start" in a for a in e)]
    assert sorted(a.split("=")[-1] for e in starts for a in e if "/start" in a) == ["1", "3"]
    # The password only ever reaches curl on stdin, never through argv.
    assert not any("word-1234" in arg for entry in state["log"] for arg in entry)
    assert 'pa\\"ss\\\\word' in state["auth"]
    (backup,) = backups.iterdir()
    assert (backup / "previous-revision").read_text().strip() == OLD
    assert (backup / "runtime-server" / "state.sqlite").read_bytes() == (
        app / "runtime" / "server" / "state.sqlite"
    ).read_bytes()
    assert (backup / "accounts-before").read_text().splitlines()[0] == "1 RUNNING 26"


def test_deploy_refuses_while_live_is_enabled_without_touching_anything(server):
    app, backups, run = server
    result, state = run(NEW, "--yes", live=True)
    assert result.returncode == 1 and "LIVE 已启用" in result.stderr
    assert not any(entry[0] == "docker" for entry in state["log"])
    assert f"PM_GIT_REVISION={OLD}" in (app / ".env").read_text() and not backups.exists()


@pytest.mark.parametrize("args", [(), ("abc",), ("C" * 40,), (NEW, NEW)])
def test_deploy_rejects_missing_or_malformed_targets(server, args):
    _, _, run = server
    result, state = run(*args)
    assert result.returncode == 1
    assert not any(entry[0] in {"docker", "git"} for entry in state["log"])


def test_deploy_requires_explicit_confirmation(server):
    _, _, run = server
    result, state = run(NEW)  # stdin is empty: no "yes"
    assert result.returncode == 1 and "已取消" in result.stderr
    assert not any(entry[0] == "docker" for entry in state["log"])


def docker_steps(state):
    return [e[-1] if e[-1] != "-d" else "up" for e in state["log"] if e[0] == "docker"]


def test_deploy_shrinks_to_account_one_and_preserves_legacy_ledger(server):
    app, backups, run = server
    legacy = app / "runtime" / "server" / "TEST" / "accounts" / "3" / "state.sqlite"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"old account bytes")
    result, state = run(
        NEW,
        "--yes",
        narrow=True,
        positions={"1": 26},
        status={"1": "RUNNING", "2": "PAUSED", "3": "PAUSED", "4": "PAUSED"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    docker = [entry for entry in state["log"] if entry[0] == "docker"]
    assert len(docker) == 4 and "run" in docker[2]
    assert "pm_nautilus.test_accounts" in docker[2]
    assert state["status"] == {"1": "RUNNING"}
    (backup,) = backups.iterdir()
    assert (
        (backup / "runtime-server" / "TEST" / "accounts" / "3" / "state.sqlite").read_bytes()
        == legacy.read_bytes()
        == b"old account bytes"
    )
    assert (backup / "accounts-before").read_text().splitlines()[-1] == "4 PAUSED 0"
    assert (backup / "accounts-after").read_text() == "1 RUNNING 26\n"

    result, state = run(OLD, "--yes", narrow=True, rollback=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert state["status"] == {"1": "RUNNING"} | {str(n): "PAUSED" for n in range(2, 11)}
    assert legacy.read_bytes() == b"old account bytes"


@pytest.mark.parametrize(
    "status, positions",
    [
        ({"1": "RUNNING", "2": "PAUSED", "3": "RUNNING", "4": "PAUSED"}, {}),
        ({"1": "RUNNING", "2": "PAUSED", "3": "PAUSED", "4": "PAUSED"}, {"3": 46}),
    ],
)
def test_deploy_refuses_to_retire_account_with_runtime_exposure(server, status, positions):
    _, backups, run = server
    result, state = run(NEW, "--yes", narrow=True, status=status, positions=positions)
    assert result.returncode == 1 and "旧 TEST 3 须暂停且没有持仓" in result.stderr
    assert not docker_steps(state) and not backups.exists()


def test_failed_archive_preflight_restores_previous_image_and_keeps_backup(server):
    app, backups, run = server
    result, state = run(
        NEW,
        "--yes",
        narrow=True,
        positions={},
        status={"1": "RUNNING", "2": "PAUSED", "3": "PAUSED", "4": "PAUSED"},
        archive_check_fail=True,
    )
    assert result.returncode == 1 and "已重新启动旧版" in result.stderr
    assert state["up"] and state["rev"] == OLD
    assert state["status"]["1"] == "RUNNING"
    assert f"PM_GIT_REVISION={OLD}" in (app / ".env").read_text()
    assert len(list(backups.glob("*/.complete"))) == 1


@pytest.mark.parametrize(
    "sim,message",
    [
        ({"live_after_build": True}, "LIVE 已启用"),
        ({"wallet_after_build": "READY"}, "LIVE 钱包当前状态为 READY"),
    ],
)
def test_deploy_rechecks_live_and_wallet_after_build_before_stopping(server, sim, message):
    app, backups, run = server
    result, state = run(NEW, "--yes", **sim)
    assert result.returncode == 1 and message in result.stderr
    assert docker_steps(state) == ["build"]  # Never stopped; old version keeps serving.
    assert state["up"] and f"PM_GIT_REVISION={OLD}" in (app / ".env").read_text()


@pytest.mark.parametrize("wallet", ["READY", "BLOCKED", "PAUSED", "ERROR", None])
def test_deploy_refuses_unlocked_or_unknown_wallet_up_front(server, wallet):
    _, _, run = server
    result, state = run(NEW, "--yes", wallet=wallet)
    assert result.returncode == 1 and "LIVE 钱包当前状态" in result.stderr
    assert not docker_steps(state)


@pytest.mark.parametrize(
    "sim", [{"non_main": True}, {"ci": "fail"}, {"ci": "pending"}, {"ci": "one"}, {"ci": "offline"}]
)
def test_deploy_refuses_unverified_commit_unless_explicitly_allowed(server, sim):
    app, _, run = server
    result, state = run(NEW, "--yes", **sim)
    assert result.returncode == 1 and "--allow-unverified" in result.stderr
    assert not docker_steps(state) and f"PM_GIT_REVISION={OLD}" in (app / ".env").read_text()
    assert not any(e[0] == "git" and "checkout" in e for e in state["log"])
    result, state = run(NEW, "--yes", "--allow-unverified", **sim)
    assert result.returncode == 0, result.stdout + result.stderr
    assert docker_steps(state) == ["build", "stop", "up"]


def test_deploy_refuses_dirty_checkout_before_building(server):
    app, _, run = server
    result, state = run(NEW, "--yes", dirty=" M src/pm_nautilus/live.py\n?? scratch.py\n")
    assert result.returncode == 1 and "未提交或未跟踪" in result.stderr
    assert not docker_steps(state) and f"PM_GIT_REVISION={OLD}" in (app / ".env").read_text()


@pytest.mark.parametrize("sim", [{"live_after_build": True}, {"wallet_after_build": "READY"}])
def test_final_safety_check_runs_after_the_last_account_query(server, sim):
    _, backups, run = server
    result, state = run(NEW, "--yes", switch_after_accounts=True, **sim)
    assert result.returncode == 1, result.stdout
    assert docker_steps(state) == ["build"] and state["up"]
    assert not backups.exists() or not any(backups.iterdir())


def test_pruning_keeps_newest_complete_backups_and_never_counts_partial_ones(server):
    app, backups, run = server
    backups.mkdir()
    for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        (backups / f"{stamp}-old").mkdir()
        (backups / f"{stamp}-old" / ".complete").touch()
    for stamp in ("20260104T000000Z", "20260105T000000Z", "20260106T000000Z"):
        (backups / f"{stamp}-partial").mkdir()  # Failed runs: no marker.
    result, _ = run(NEW, "--yes", **{})
    assert result.returncode == 0, result.stdout + result.stderr
    names = sorted(path.name for path in backups.iterdir())
    new = [name for name in names if name.endswith(NEW[:7])]
    assert len(new) == 1 and (backups / new[0] / ".complete").exists()
    # 4 complete backups exist and the default keeps 5: nothing complete is pruned,
    # and partial directories are neither counted nor deleted.
    assert {"20260101T000000Z-old", "20260104T000000Z-partial"} <= set(names)
    result, _ = run(OLD, "--yes")
    result, _ = run(NEW, "--yes")
    result, _ = run(OLD, "--yes")
    names = sorted(path.name for path in backups.iterdir())
    complete = [n for n in names if (backups / n / ".complete").exists()]
    assert len(complete) == 5 and "20260101T000000Z-old" not in names
    assert {"20260104T000000Z-partial", "20260105T000000Z-partial"} <= set(names)


def test_a_second_concurrent_deploy_is_refused(server, tmp_path):
    app, _, run = server
    holder = subprocess.Popen(["flock", str(tmp_path / "deploy.lock"), "sleep", "30"])
    try:
        for _ in range(50):  # Wait until the holder owns the lock.
            if subprocess.run(["flock", "-n", str(tmp_path / "deploy.lock"), "true"]).returncode:
                break
            __import__("time").sleep(0.1)
        result, state = run(NEW, "--yes")
    finally:
        holder.kill()
        holder.wait()
    assert result.returncode == 1 and "另一个部署正在进行" in result.stderr
    assert not any(entry[0] in {"docker", "git"} for entry in state["log"])
    assert f"PM_GIT_REVISION={OLD}" in (app / ".env").read_text()
