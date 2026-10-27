"""Synchronous account views and PAUSE remain usable during asynchronous validation."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from pm_nautilus.app import create_app


@pytest.fixture(autouse=True)
def local_test_only(monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("PM_UI_PASSWORD", raising=False)


@pytest.mark.parametrize("operation", ["validation", "start"])
def test_sync_views_and_pause_stay_responsive_during_account_validation(
    tmp_path, monkeypatch, operation
):
    async def run():
        app = create_app(tmp_path, public_data=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                initial = await client.get("/api/dashboard?account_id=2")
                client.headers["x-pm-csrf"] = initial.headers["x-pm-csrf"]
                runtime = app.state.test_runtimes[2]
                original = runtime.validate_async
                entered, release = asyncio.Event(), asyncio.Event()

                async def gated_validation():
                    entered.set()
                    await release.wait()
                    return await original()

                monkeypatch.setattr(runtime, "validate_async", gated_validation)
                pending = asyncio.create_task(
                    client.get("/api/TEST/validation?account_id=2")
                    if operation == "validation"
                    else client.post("/api/TEST/start?account_id=2")
                )
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    for endpoint in ("/api/dashboard", "/api/TEST/preferences"):
                        result = await asyncio.wait_for(client.get(f"{endpoint}?account_id=2"), 1)
                        assert result.status_code == 200
                        assert result.json()["accountId"] == 2
                    paused = await asyncio.wait_for(client.post("/api/TEST/pause?account_id=2"), 1)
                    assert paused.status_code == 200
                    assert paused.json()["strategy"]["status"] == "PAUSED"
                    assert not pending.done()
                    release.set()
                    result = await asyncio.wait_for(pending, 3)
                    if operation == "start":
                        assert result.status_code == 400
                        assert "启动核对期间状态已更改" in result.json()["error"]
                        assert runtime.status == "PAUSED"
                    else:
                        assert result.status_code == 200
                        assert result.json()["ok"] is True
                finally:
                    release.set()
                    await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(run())


def test_sync_views_and_pause_are_gated_during_account_reset(tmp_path, monkeypatch):
    async def run():
        app = create_app(tmp_path, public_data=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                initial = await client.get("/api/dashboard?account_id=2")
                client.headers["x-pm-csrf"] = initial.headers["x-pm-csrf"]
                service = app.state.test_services[2]
                original_close = service.close
                entered, release = asyncio.Event(), asyncio.Event()

                async def gated_close():
                    entered.set()
                    await release.wait()
                    await original_close()

                monkeypatch.setattr(service, "close", gated_close)
                pending = asyncio.create_task(
                    client.post(
                        "/api/TEST/reset?account_id=2",
                        json={
                            "confirmation": "RESET TEST",
                            "finalConfirmation": "RESET TEST AGAIN",
                        },
                    )
                )
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    for endpoint in ("/api/dashboard", "/api/TEST/preferences"):
                        result = await asyncio.wait_for(client.get(f"{endpoint}?account_id=2"), 1)
                        assert result.status_code == 503
                        assert "正在重置" in result.json()["detail"]
                    assert (
                        await asyncio.wait_for(client.post("/api/TEST/pause?account_id=2"), 1)
                    ).status_code == 503
                    assert (
                        await asyncio.wait_for(client.get("/api/dashboard?account_id=3"), 1)
                    ).status_code == 200
                    assert not pending.done()
                    release.set()
                    assert (await asyncio.wait_for(pending, 3)).status_code == 200
                    assert (await client.get("/api/dashboard?account_id=2")).status_code == 200
                finally:
                    release.set()
                    await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(run())


def test_legacy_validation_shape_and_explicit_account_identity(tmp_path):
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        expected = {"ok": True, "errors": [], "mode": "TEST"}
        assert client.get("/api/TEST/validation").json() == expected
        for number in (1, 10):
            assert client.get(f"/api/TEST/validation?account_id={number}").json() == expected | {
                "accountId": number
            }
