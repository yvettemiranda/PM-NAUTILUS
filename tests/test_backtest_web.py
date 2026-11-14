import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from pm_nautilus.app import create_app
from pm_nautilus.backtest_web import UPLOAD_CHUNK
from pm_nautilus.config import Preferences


SAMPLE = Path(__file__).parent.parent / "src" / "pm_nautilus" / "web" / "backtest-sample.ndjson"


def test_authenticated_replay_runs_in_worker_without_changing_test_account(monkeypatch, tmp_path):
    monkeypatch.setenv("PM_UI_PASSWORD", "local-integration-only")
    monkeypatch.delenv("PM_TEST_ACTIVE_ACCOUNT", raising=False)
    app = create_app(tmp_path, public_data=False)
    with TestClient(app, base_url="http://localhost") as c:
        assert c.get("/api/backtest/datasets").status_code == 401
        assert c.get("/backtest.html").status_code == 401
        c.auth = ("pm", "local-integration-only")
        response = c.get("/api/backtest/defaults")
        headers = {"x-pm-csrf": response.headers["x-pm-csrf"]}
        assert response.json()["initialCash"] == "100"
        assert c.post("/api/backtest/uploads").status_code == 403

        tape = SAMPLE.read_bytes()
        upload = c.post("/api/backtest/uploads", headers=headers).json()["id"]
        archive_url = f"/api/backtest/uploads/{upload}"
        too_large = c.put(
            archive_url, params={"offset": 0}, content=b"x" * (UPLOAD_CHUNK + 1), headers=headers
        )
        assert too_large.status_code == 413
        for start, chunk in [(0, tape[:50]), (50, tape[50:])]:
            response = c.put(archive_url, params={"offset": start}, content=chunk, headers=headers)
            assert response.json()["receivedBytes"] == start + len(chunk)
        assert (
            c.put(archive_url, params={"offset": 0}, content=tape, headers=headers).status_code
            == 409
        )
        info = c.post(f"{archive_url}/finish", headers=headers).json()["dataset"]
        assert c.get("/api/backtest/datasets").json()["datasets"] == [info]

        response = c.post(
            "/api/backtest/jobs",
            json={
                "sourceId": info["id"],
                "startMs": info["startMs"],
                "endMs": info["endMs"],
                "initialCash": "1",
                "preferences": Preferences().model_dump(mode="json"),
            },
            headers=headers,
        )
        assert response.status_code == 202
        job = response.json()["id"]
        for _ in range(150):
            state = c.get(f"/api/backtest/jobs/{job}").json()
            if state["status"] != "running":
                break
            time.sleep(0.05)
        assert state["status"] == "completed", state
        assert state["result"]["summary"]["endingCash"] == "22.5"
        assert state["result"]["summary"]["fillCount"] == 3
        assert c.get("/api/backtest/defaults").json()["initialCash"] == "100"
        assert c.get("/api/dashboard?details=summary").json()["positionCount"] == 0
        assert not list((tmp_path / "backtest" / "jobs" / job).glob("pm-backtest-*"))


def test_malformed_package_is_not_published(tmp_path):
    with TestClient(create_app(tmp_path, public_data=False), base_url="http://localhost") as c:
        headers = {"x-pm-csrf": c.get("/api/backtest/datasets").headers["x-pm-csrf"]}
        bad_header = {"type": "header", "version": 1, "source": "incomplete"}
        response = c.put("/api/backtest/datasets", content=json.dumps(bad_header), headers=headers)
        assert response.status_code == 400
        assert c.get("/api/backtest/datasets").json()["datasets"] == []
