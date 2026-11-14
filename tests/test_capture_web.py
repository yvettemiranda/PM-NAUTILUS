import hashlib
import io
import tarfile
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pm_nautilus.capture import CaptureRecorder
from pm_nautilus.capture_web import capture_router
from pm_nautilus.web_security import install_security


def test_authenticated_daily_export_and_checksum_release(tmp_path):
    recorder = CaptureRecorder(tmp_path, min_free_bytes=0, min_free_percent=0)
    app = FastAPI()
    install_security(
        app,
        password="test-capture-password",
        allowed={"localhost"},
        csrf="csrf",
        is_shutting_down=lambda: False,
    )
    app.include_router(capture_router(recorder))
    base = "/api/backtest/capture"
    start = 1_794_600_000_000
    day = datetime.fromtimestamp(start / 1000, timezone(timedelta(hours=8))).date()

    async def record():
        import asyncio

        recorder.start()
        async with asyncio.timeout(3):
            while not recorder.accepting:
                await asyncio.sleep(0.005)
        for at in (start, start + 10):
            assert recorder.submit({"type": "control", "atMs": at, "status": "RUNNING"})
        return await recorder.seal()

    with TestClient(app, base_url="http://localhost") as client:
        assert client.get(base).status_code == 401
        client.auth = ("pm", "test-capture-password")
        try:
            archive = client.portal.call(record)
            name = archive["id"]
            state = client.get(base).json()
            assert state["status"] == "recording"
            assert state["days"][0]["day"] == day.isoformat()
            assert client.post(base + "/seal").status_code == 403
            download = client.get(f"{base}/days/{day}")
            assert download.status_code == 200
            with tarfile.open(fileobj=io.BytesIO(download.content)) as bundle:
                assert bundle.getnames() == [name]
                stream = bundle.extractfile(name)
                assert stream is not None
                payload = stream.read()
            assert client.get(f"{base}/archives/{name}").content == payload
            assert client.get(f"{base}/archives/arbitrary.gz").status_code == 404
            headers = {"X-PM-CSRF": "csrf"}
            assert (
                client.post(
                    base + "/release", headers=headers, json={"id": name, "sha256": "0" * 64}
                ).status_code
                == 409
            )
            assert (
                client.post(
                    base + "/release",
                    headers=headers,
                    json={"id": name, "sha256": hashlib.sha256(payload).hexdigest()},
                ).status_code
                == 200
            )
            assert client.get(base).json()["days"] == []
        finally:
            client.portal.call(recorder.close)
