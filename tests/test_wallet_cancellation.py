"""Cancelled wallet requests retain ownership until their state changes finish."""

import asyncio
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from threading import Event

import httpx
import pytest
from starlette.requests import Request

import pm_nautilus.app as app_module
import pm_nautilus.live_backup as backup
from pm_nautilus.store import Store
from pm_nautilus.strategy import Runtime
from pm_nautilus.wallet_vault import write_vault
from test_lifecycle import controlled_factory
from test_live_backup import PASSWORD, _settings as backup_settings
from test_live_wallet_api import _payload, _settings


def request(body, content_type="application/json"):
    if isinstance(body, dict):
        body = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "path": "/",
            "headers": [(b"content-type", content_type.encode())],
        },
        receive,
    )


def endpoint(app, path):
    return next(route.endpoint for route in app.routes if route.path == path)


def configure(monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("PM_UI_PASSWORD", raising=False)
    monkeypatch.setenv("PM_ALLOWED_HOSTS", "testserver")
    monkeypatch.setattr(app_module, "run_readiness", lambda *_args, **_kwargs: {"ready": True})


@pytest.mark.parametrize("transport", ["endpoint", "asgi"])
def test_cancelled_enable_closes_partial_runtime_even_if_cancelled_again(
    tmp_path, monkeypatch, transport
):
    configure(monkeypatch)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    monkeypatch.setattr("pm_nautilus.live.live_factory", lambda *_: controlled_factory)
    monkeypatch.setattr(
        "pm_nautilus.redemption.PolygonWallet",
        lambda _: SimpleNamespace(preflight=lambda: None),
    )

    async def run():
        activation_entered = asyncio.Event()
        cleanup_entered = asyncio.Event()
        cleanup_release = asyncio.Event()
        live = []

        def runtime(*args, **kwargs):
            owner = Runtime(*args, **kwargs)
            if owner.mode == "LIVE":
                live.append(owner)
                shutdown = owner.client.shutdown

                async def activate():
                    activation_entered.set()
                    await asyncio.Future()

                async def finish_shutdown():
                    cleanup_entered.set()
                    await cleanup_release.wait()
                    owner.store.put("cancelled_activation_cleanup", True)
                    await shutdown()

                owner.client.activate = activate
                owner.client.shutdown = finish_shutdown
            return owner

        monkeypatch.setattr(app_module, "Runtime", runtime)
        app = app_module.create_app(tmp_path, public_data=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                csrf = (await client.get("/api/dashboard")).headers["x-pm-csrf"]
                headers = {"x-pm-csrf": csrf}
                imported = await client.post(
                    "/api/live/wallet/import", json=_payload(), headers=headers
                )
                assert imported.status_code == 200
                path = "/api/live/wallet/enable"
                payload = {"confirmation": "ENABLE LIVE"}
                operation = (
                    endpoint(app, path)(request(payload))
                    if transport == "endpoint"
                    else client.post(path, json=payload, headers=headers)
                )
                task = asyncio.create_task(operation)
                try:
                    await asyncio.wait_for(activation_entered.wait(), 5)
                    assert "LIVE" not in app.state.runtimes
                    assert (await client.get("/api/live/wallet")).json()["enabled"] is False
                    task.cancel()
                    await asyncio.wait_for(cleanup_entered.wait(), 5)
                    task.cancel()
                    await asyncio.sleep(0)
                    assert not task.done() and not live[0].store._closed
                    cleanup_release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 5)
                    owner = live[0]
                    assert owner.store._closed
                    assert owner.native.execution.get_cmd_queue_task().done()
                    assert owner.native.execution.get_evt_queue_task().done()
                    assert set(app.state.runtimes) == {"TEST"}
                    assert (await client.get("/api/dashboard?mode=LIVE")).status_code == 200
                    assert (await client.get("/api/live/wallet")).json()["enabled"] is False
                    with closing(sqlite3.connect(owner.store.path)) as db:
                        assert (
                            db.execute(
                                "SELECT value FROM meta WHERE key='cancelled_activation_cleanup'"
                            ).fetchone()[0]
                            == "true"
                        )
                finally:
                    cleanup_release.set()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["endpoint", "asgi"])
def test_cancelled_restore_holds_wallet_lock_and_upload_until_worker_finishes(
    tmp_path, monkeypatch, transport
):
    configure(monkeypatch)
    source = Store(tmp_path / "source/LIVE/state.sqlite", "LIVE")
    source_vault = tmp_path / "source/wallet/live.vault"
    identity = write_vault(source_vault, backup_settings(), PASSWORD)
    source.put("live_wallet_identity", identity)
    source.save_intent(
        "restored-history", {"event_id": "e", "token_id": "1", "side": "BUY", "terminal": True}
    )
    generation = source.generation
    bundle = backup.export_live_bundle(source, source_vault, PASSWORD)
    source.close()
    entered, release, completed = Event(), Event(), Event()
    write = backup.write_vault
    restore = backup.restore_live_bundle_file

    def gated_write(*args, **kwargs):
        result = write(*args, **kwargs)
        entered.set()
        if not release.wait(10):
            raise RuntimeError("Controlled restore pause timed out")
        return result

    def observed_restore(*args, **kwargs):
        try:
            return restore(*args, **kwargs)
        finally:
            completed.set()

    monkeypatch.setattr(backup, "write_vault", gated_write)
    monkeypatch.setattr(backup, "restore_live_bundle_file", observed_restore)

    async def run():
        destination = tmp_path / "destination"
        app = app_module.create_app(destination, public_data=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                csrf = (await client.get("/api/dashboard")).headers["x-pm-csrf"]
                headers = {"x-pm-csrf": csrf}
                path = "/api/live/wallet/restore"
                password = PASSWORD.encode()
                body = len(password).to_bytes(2, "big") + password + bundle
                content_type = "application/octet-stream"
                operation = (
                    endpoint(app, path)(request(body, content_type))
                    if transport == "endpoint"
                    else client.post(
                        path, content=body, headers=headers | {"content-type": content_type}
                    )
                )
                task = asyncio.create_task(operation)
                unlocked = removed = None
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    task.cancel()
                    await asyncio.sleep(0)
                    task.cancel()
                    await asyncio.sleep(0)
                    assert not task.done() and not completed.is_set()
                    assert list((destination / "LIVE").glob(".pm-live-upload-*"))
                    unlocked = asyncio.create_task(
                        client.post(
                            "/api/live/wallet/unlock",
                            json={"vaultPassword": PASSWORD},
                            headers=headers,
                        )
                    )
                    # Once the request has parsed its body it must wait on the restore lock.
                    for _ in range(10):
                        await asyncio.sleep(0)
                    assert not unlocked.done()
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 5)
                    assert completed.is_set()
                    assert (await asyncio.wait_for(unlocked, 5)).status_code == 200
                    removed = await client.post(
                        "/api/live/wallet/remove",
                        json={"vaultPassword": PASSWORD, "confirmation": "REPLACE EMPTY WALLET"},
                        headers=headers,
                    )
                    assert removed.status_code == 400
                    assert (destination / "wallet/live.vault").is_file()
                    assert not list((destination / "LIVE").glob(".pm-live-upload-*"))
                    with closing(sqlite3.connect(destination / "LIVE/state.sqlite")) as db:
                        assert (
                            json.loads(
                                db.execute(
                                    "SELECT value FROM meta WHERE key='live_wallet_identity'"
                                ).fetchone()[0]
                            )
                            == identity
                        )
                        assert (
                            json.loads(
                                db.execute(
                                    "SELECT value FROM meta WHERE key='generation'"
                                ).fetchone()[0]
                            )
                            == generation
                        )
                        assert (
                            db.execute(
                                "SELECT count(*) FROM intents WHERE order_id='restored-history'"
                            ).fetchone()[0]
                            == 1
                        )
                finally:
                    release.set()
                    await asyncio.gather(
                        task, *(x for x in (unlocked,) if x), return_exceptions=True
                    )
                    await asyncio.to_thread(completed.wait, 5)

    asyncio.run(run())
