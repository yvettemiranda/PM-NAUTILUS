"""deploy/deploy.sh against stub docker/git/curl: order, backups, restore, refusals."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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
    if "stop" in args:
        s["up"] = False
    if "up" in args:
        env = dict(l.strip().split("=", 1) for l in open(".env") if "=" in l)
        s["up"], s["rev"] = True, env["PM_GIT_REVISION"]
        s["status"] = {k: "PAUSED" for k in s["status"]}
    save(); sys.exit(0)
if tool == "git":
    if "rev-parse" in args:
        print("b" * 40)
    sys.exit(0)
if "-K" in args:
    s["auth"] = sys.stdin.read(); save()
url = [a for a in args if a.startswith("http")][0]
if not s["up"]:
    sys.exit(7)
if "-D" in args:
    print("HTTP/1.1 200 OK\r\nx-pm-csrf: tok\r\n\r"); sys.exit(0)
if url.endswith("/api/health"):
    print(json.dumps({
        "status": "ok", "revision": s["rev"], "testAccountCount": 4,
        "availableTestAccountCount": 4, "liveExecutionEnabled": s["live"],
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
                  "positionCount": {"1": 26, "3": 46}.get(account, 0), "error": None}))
"""


@pytest.fixture
def server(tmp_path):
    bin_dir, app = tmp_path / "bin", tmp_path / "app"
    bin_dir.mkdir()
    (app / "runtime" / "server").mkdir(parents=True)
    (app / "runtime" / "server" / "state.sqlite").write_bytes(os.urandom(4096))
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
        json.dumps({"up": True, "rev": OLD, "live": False, "status": status, "log": []})
    )

    def run(*args, live=False):
        data = json.loads(state.read_text())
        data["live"] = live
        state.write_text(json.dumps(data))
        environ = os.environ | {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SIM_STATE": str(state),
            "PM_APP_DIR": str(app),
            "PM_BACKUP_ROOT": str(tmp_path / "backups"),
            "PM_HEALTH_TIMEOUT": "6",
            "PM_MARKET_TIMEOUT": "6",
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
