"""Run inside a disposable container before/after same-volume restart; never START."""

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("phase", choices=["first", "restart"])
parser.add_argument("--expected-revision", required=True)
args = parser.parse_args()
base = "http://127.0.0.1:8765"
with httpx.Client(base_url=base, timeout=5) as public:
    for attempt in range(30):
        try:
            health = public.get("/api/health")
            if health.status_code == 200:
                break
        except httpx.TransportError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("Container did not become healthy")
    status = health.json()
    assert status["revision"] == args.expected_revision, status
    assert status["strategyStatus"] == "PAUSED" and not status["liveExecutionEnabled"]
    assert public.get("/api/dashboard").status_code == 401
with httpx.Client(base_url=base, auth=("pm", os.environ["PM_UI_PASSWORD"]), timeout=5) as client:
    response = client.get("/api/dashboard")
    response.raise_for_status()
    data = response.json()
    assert data["executionMode"] == "TEST" and data["strategy"]["status"] == "PAUSED"
    assert data["portfolio"]["availableCash"] == "100" and not data["positions"]
    assert data["preferences"]["orderAmount"] == ("1" if args.phase == "first" else "2")
    assert client.get("/api/LIVE/preferences").json()["preferences"]["orderAmount"] == "1"
    assert "PM-NAUTILUS" in client.get("/").text
    assert "loadTradeRecords" in client.get("/app.js").text
    assert client.get("/api/TEST/validation").json()["ok"]
    changed = client.put(
        "/api/TEST/preferences",
        json={"orderAmount": "2" if args.phase == "first" else "1"},
        headers={"x-pm-csrf": response.headers["x-pm-csrf"]},
    )
    changed.raise_for_status()
    database = Path(os.environ["PM_DATA_DIR"]) / "TEST" / "state.sqlite"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        generation = json.loads(
            connection.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
        )
    print(
        json.dumps(
            {
                "phase": args.phase,
                "health": status,
                "generation": generation,
                "saved_order_amount": changed.json()["preferences"]["orderAmount"],
                "cash": "100",
                "authenticated_api_and_ui": "passed",
                "mode_isolation": "passed",
            },
            indent=2,
        )
    )
