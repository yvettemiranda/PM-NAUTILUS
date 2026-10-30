"""Wallet import retains threads, binding and process ownership through cancellation."""

import asyncio
import fcntl
import json
import sqlite3
from contextlib import closing
from threading import Event

import httpx
import pytest

import pm_nautilus.app as app_module
from pm_nautilus.store import Store
from pm_nautilus.wallet_vault import VaultError, read_public_metadata
from test_live_wallet_api import ADDRESS, _payload, _settings
from test_wallet_cancellation import configure, endpoint, request


def binding(root):
    with closing(sqlite3.connect(root / "LIVE/state.sqlite")) as db:
        row = db.execute("SELECT value FROM meta WHERE key='live_wallet_identity'").fetchone()
        return json.loads(row[0]) if row else None


def process_lock_held(root):
    with (root / "process.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
        return False


async def cancel_twice(task):
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)


def import_operation(app, client, headers, payload, transport):
    if transport == "endpoint":
        return endpoint(app, "/api/live/wallet/import")(request(payload))
    return client.post("/api/live/wallet/import", json=payload, headers=headers)


@pytest.mark.parametrize("transport", ["endpoint", "asgi"])
@pytest.mark.parametrize("stage", ["before_publish", "after_publish", "readiness"])
def test_cancelled_import_finishes_binding_before_wallet_or_shutdown_unlocks(
    tmp_path, monkeypatch, transport, stage
):
    configure(monkeypatch)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    original_write = app_module.write_vault
    entered, release, completed = Event(), Event(), Event()

    def gate():
        entered.set()
        assert release.wait(10)

    def write(*args, **kwargs):
        try:
            if stage == "before_publish":
                gate()
            result = original_write(*args, **kwargs)
            if stage == "after_publish":
                gate()
            return result
        finally:
            if stage != "readiness":
                completed.set()

    def readiness(*_args, **_kwargs):
        try:
            gate()
            return {"ready": True, "checks": []}
        finally:
            completed.set()

    monkeypatch.setattr(app_module, "write_vault", write)
    if stage == "readiness":
        monkeypatch.setattr(app_module, "run_readiness", readiness)

    async def run():
        app = app_module.create_app(tmp_path, public_data=False)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        task = queued = shutting_down = None
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                headers = {"x-pm-csrf": (await client.get("/api/dashboard")).headers["x-pm-csrf"]}
                task = asyncio.create_task(
                    import_operation(app, client, headers, _payload(), transport)
                )
                assert await asyncio.to_thread(entered.wait, 5)
                vault = tmp_path / "wallet/live.vault"
                published = vault.read_bytes() if vault.exists() else None
                await cancel_twice(task)
                assert not task.done() and not completed.is_set()
                # A second import must wait, then refuse to replace the first wallet.
                queued = asyncio.create_task(
                    client.post("/api/live/wallet/import", json=_payload(), headers=headers)
                )
                for _ in range(10):
                    await asyncio.sleep(0)
                assert not queued.done()
                shutting_down = asyncio.create_task(lifespan.__aexit__(None, None, None))
                for _ in range(10):
                    await asyncio.sleep(0)
                assert not shutting_down.done() and process_lock_held(tmp_path)
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                refused = await asyncio.wait_for(queued, 5)
                assert refused.status_code == 400 and "钱包已经配置" in refused.json()["error"]
                await asyncio.wait_for(shutting_down, 5)
                assert completed.is_set() and not process_lock_held(tmp_path)
                assert binding(tmp_path) == read_public_metadata(vault)
                assert published is None or vault.read_bytes() == published
                assert "LIVE" not in app.state.runtimes
                with closing(sqlite3.connect(tmp_path / "LIVE/state.sqlite")) as db:
                    assert db.execute("SELECT count(*) FROM intents").fetchone()[0] == 0
                    assert db.execute("SELECT count(*) FROM native_events").fetchone()[0] == 0
        finally:
            release.set()
            await asyncio.gather(*(x for x in (task, queued) if x), return_exceptions=True)
            if shutting_down is None:
                await lifespan.__aexit__(None, None, None)
            else:
                await asyncio.gather(shutting_down, return_exceptions=True)
            if entered.is_set():
                assert await asyncio.to_thread(completed.wait, 5)

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["endpoint", "asgi"])
@pytest.mark.parametrize("stage", ["discovery", "credentials"])
def test_cancelled_preparation_waits_current_thread_without_starting_vault_write(
    tmp_path, monkeypatch, transport, stage
):
    configure(monkeypatch)
    entered, release, completed = Event(), Event(), Event()
    calls = {"credentials": 0, "write": 0}

    def gate():
        entered.set()
        assert release.wait(10)

    def discover(*_args, **_kwargs):
        try:
            gate()
            return ADDRESS
        finally:
            completed.set()

    def credentials(*args, **kwargs):
        calls["credentials"] += 1
        try:
            if stage == "credentials":
                gate()
            return _settings(*args, **kwargs)
        finally:
            if stage == "credentials":
                completed.set()

    def write(*_args, **_kwargs):
        calls["write"] += 1
        raise AssertionError("Cancelled preparation must not publish a vault")

    monkeypatch.setattr("pm_nautilus.live_web_setup.discover_funder", discover)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", credentials)
    monkeypatch.setattr(app_module, "write_vault", write)

    async def run():
        app = app_module.create_app(tmp_path, public_data=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                headers = {"x-pm-csrf": (await client.get("/api/dashboard")).headers["x-pm-csrf"]}
                payload = _payload()
                if stage == "discovery":
                    payload["publicAddress"] = None
                task = asyncio.create_task(
                    import_operation(app, client, headers, payload, transport)
                )
                queued = None
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    await cancel_twice(task)
                    assert not task.done() and not completed.is_set()
                    queued = asyncio.create_task(
                        client.post(
                            "/api/live/wallet/unlock",
                            json={"vaultPassword": payload["vaultPassword"]},
                            headers=headers,
                        )
                    )
                    for _ in range(10):
                        await asyncio.sleep(0)
                    assert not queued.done()
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 5)
                    assert (await asyncio.wait_for(queued, 5)).status_code == 400
                    assert completed.is_set() and calls["write"] == 0
                    assert calls["credentials"] == (stage == "credentials")
                    assert not (tmp_path / "wallet/live.vault").exists()
                    assert binding(tmp_path) is None
                finally:
                    release.set()
                    await asyncio.gather(task, *(x for x in (queued,) if x), return_exceptions=True)
                    if entered.is_set():
                        assert await asyncio.to_thread(completed.wait, 5)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["write", "published_write", "binding"])
def test_cancelled_import_failure_remains_recoverable(tmp_path, monkeypatch, failure):
    configure(monkeypatch)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    entered, release, completed = Event(), Event(), Event()
    original_write, original_put = app_module.write_vault, Store.put
    fail_binding = failure == "binding"

    def write(*args, **kwargs):
        try:
            entered.set()
            assert release.wait(10)
            if failure == "write":
                raise VaultError("Controlled wallet write failure")
            metadata = original_write(*args, **kwargs)
            if failure == "published_write":
                raise VaultError("Controlled failure after wallet publication")
            return metadata
        finally:
            completed.set()

    def put(store, key, value):
        nonlocal fail_binding
        if key == "live_wallet_identity" and fail_binding:
            fail_binding = False
            raise sqlite3.OperationalError("Controlled wallet binding failure")
        return original_put(store, key, value)

    monkeypatch.setattr(app_module, "write_vault", write)
    monkeypatch.setattr(Store, "put", put)

    async def run():
        app = app_module.create_app(tmp_path, public_data=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                headers = {"x-pm-csrf": (await client.get("/api/dashboard")).headers["x-pm-csrf"]}
                task = asyncio.create_task(
                    client.post("/api/live/wallet/import", json=_payload(), headers=headers)
                )
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    await cancel_twice(task)
                    assert not task.done()
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 5)
                    assert completed.is_set() and binding(tmp_path) is None
                    monkeypatch.setattr(app_module, "write_vault", original_write)
                    if failure == "write":
                        assert not (tmp_path / "wallet/live.vault").exists()
                        recovered = await client.post(
                            "/api/live/wallet/import", json=_payload(), headers=headers
                        )
                    else:
                        state = (await client.get("/api/live/wallet")).json()
                        assert state["configured"]
                        if failure == "binding":
                            assert state["status"] == "ERROR"
                            assert "账本绑定未完成" in state["error"]
                        else:
                            assert state["status"] == "LOCKED"
                        recovered = await client.post(
                            "/api/live/wallet/unlock",
                            json={"vaultPassword": _payload()["vaultPassword"]},
                            headers=headers,
                        )
                    assert recovered.status_code == 200
                    assert binding(tmp_path) == read_public_metadata(tmp_path / "wallet/live.vault")
                    assert recovered.json()["enabled"] is False
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
                    if entered.is_set():
                        assert await asyncio.to_thread(completed.wait, 5)

    asyncio.run(run())
