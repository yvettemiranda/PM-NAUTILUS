"""Backup/authorization cancellation owns workers and releases private scratch."""

import asyncio
import fcntl
import json
import os
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path
from threading import Event

import httpx
import pytest
from eth_account import Account

import pm_nautilus.app as app_module
import pm_nautilus.live_backup as backup
from pm_nautilus.redemption import APPROVAL_KEY
from pm_nautilus.store import Store
from pm_nautilus.wallet_vault import write_vault
from test_live_backup import PASSWORD, _settings as backup_settings
from test_live_wallet_api import _payload, _settings
from test_live_secret_bundle import bundle_module
from test_redemption_approval import ControlledWallet
from test_wallet_cancellation import configure, endpoint, request


@pytest.mark.parametrize("transport", ["endpoint", "asgi"])
def test_cancelled_backup_keeps_lock_joins_twice_and_removes_finished_bundle(
    tmp_path, monkeypatch, transport
):
    configure(monkeypatch)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    entered, release, completed = Event(), Event(), Event()
    encrypt = backup._encrypt_file

    def gated_encrypt(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(10)
            return encrypt(*args, **kwargs)
        finally:
            completed.set()

    monkeypatch.setattr(backup, "_encrypt_file", gated_encrypt)

    async def run():
        app = app_module.create_app(tmp_path, public_data=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                headers = {"x-pm-csrf": (await client.get("/api/dashboard")).headers["x-pm-csrf"]}
                assert (
                    await client.post("/api/live/wallet/import", json=_payload(), headers=headers)
                ).status_code == 200
                path = "/api/live/wallet/backup"
                payload = {"vaultPassword": _payload()["vaultPassword"]}
                operation = (
                    endpoint(app, path)(request(payload))
                    if transport == "endpoint"
                    else client.post(path, json=payload, headers=headers)
                )
                task = asyncio.create_task(operation)
                following = None
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    task.cancel()
                    await asyncio.sleep(0)
                    task.cancel()
                    following = asyncio.create_task(
                        client.post("/api/live/wallet/check", json={}, headers=headers)
                    )
                    for _ in range(10):
                        await asyncio.sleep(0)
                    assert not task.done() and not following.done() and not completed.is_set()
                    assert list((tmp_path / "LIVE").glob(".pm-live-export-*"))
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 5)
                    assert completed.is_set()
                    assert (await asyncio.wait_for(following, 5)).status_code == 200
                    assert not list((tmp_path / "LIVE").glob(".pm-live-export-*"))
                    downloaded = await client.post(path, json=payload, headers=headers)
                    assert downloaded.status_code == 200 and downloaded.content.startswith(
                        backup.MAGIC_V2
                    )
                    assert not list((tmp_path / "LIVE").glob(".pm-live-export-*"))
                finally:
                    release.set()
                    await asyncio.gather(
                        task, *(x for x in (following,) if x), return_exceptions=True
                    )

    asyncio.run(run())


@pytest.mark.parametrize("next_action", ["approve", "remove"])
def test_cancelled_authorization_keeps_wallet_and_worker_owned(tmp_path, monkeypatch, next_action):
    configure(monkeypatch)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    wallet = ControlledWallet()
    entered, release, completed = Event(), Event(), Event()
    prepare = wallet.prepare_approval

    def gated_prepare(neg_risk):
        entered.set()
        try:
            assert release.wait(10)
            return prepare(neg_risk)
        finally:
            completed.set()

    wallet.prepare_approval = gated_prepare
    monkeypatch.setattr("pm_nautilus.redemption.PolygonWallet", lambda _: wallet)

    async def run():
        app = app_module.create_app(tmp_path, public_data=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                headers = {"x-pm-csrf": (await client.get("/api/dashboard")).headers["x-pm-csrf"]}
                assert (
                    await client.post("/api/live/wallet/import", json=_payload(), headers=headers)
                ).status_code == 200
                path = "/api/live/wallet/approve-redemption"
                payload = {"confirmation": "APPROVE REDEMPTION"}
                task = asyncio.create_task(endpoint(app, path)(request(payload)))
                following = None
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    task.cancel()
                    await asyncio.sleep(0)
                    task.cancel()
                    if next_action == "approve":
                        following = asyncio.create_task(
                            client.post(path, json=payload, headers=headers)
                        )
                    else:
                        following = asyncio.create_task(
                            client.post(
                                "/api/live/wallet/remove",
                                headers=headers,
                                json={
                                    "confirmation": "REPLACE EMPTY WALLET",
                                    "vaultPassword": _payload()["vaultPassword"],
                                },
                            )
                        )
                    for _ in range(10):
                        await asyncio.sleep(0)
                    assert not task.done() and not following.done() and not completed.is_set()
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 5)
                    result = await asyncio.wait_for(following, 5)
                    assert result.status_code == (200 if next_action == "approve" else 400)
                    assert completed.is_set() and (tmp_path / "wallet/live.vault").is_file()
                    assert wallet.prepared == [False]
                    assert len(set(wallet.broadcasts)) == 1
                    with closing(sqlite3.connect(tmp_path / "LIVE/state.sqlite")) as db:
                        row = db.execute(
                            "SELECT value FROM meta WHERE key=?", (APPROVAL_KEY,)
                        ).fetchone()
                        assert row is not None
                        assert (
                            db.execute(
                                "SELECT value FROM meta WHERE key='live_wallet_identity'"
                            ).fetchone()[0]
                            != "null"
                        )
                finally:
                    release.set()
                    await asyncio.gather(
                        task, *(x for x in (following,) if x), return_exceptions=True
                    )

    asyncio.run(run())


def test_export_closes_all_snapshot_connections_before_gc(tmp_path, monkeypatch):
    store = Store(tmp_path / "LIVE/state.sqlite", "LIVE")
    vault = tmp_path / "wallet/live.vault"
    store.put("live_wallet_identity", write_vault(vault, backup_settings(), PASSWORD))
    connect = sqlite3.connect
    connections = []

    def tracked_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        connections.append(db)
        return db

    monkeypatch.setattr(backup.sqlite3, "connect", tracked_connect)
    try:
        output = backup.export_live_bundle_file(store, vault, PASSWORD)
        assert len(connections) == 3
        for db in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                db.execute("SELECT 1")
        backup.cleanup_live_bundle_file(output)
    finally:
        store.close()


def test_empty_wallet_replacement_clears_complete_approval_but_keeps_signer_nonce_floor(
    tmp_path, monkeypatch
):
    configure(monkeypatch)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)

    async def run():
        app = app_module.create_app(tmp_path, public_data=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                headers = {"x-pm-csrf": (await client.get("/api/dashboard")).headers["x-pm-csrf"]}
                payload = _payload()
                assert (
                    await client.post("/api/live/wallet/import", json=payload, headers=headers)
                ).status_code == 200
                record = {
                    "version": 1,
                    "status": "COMPLETE",
                    "pending": None,
                    "remaining": [],
                    "identity": {
                        "signer": payload["publicAddress"],
                        "funder": payload["publicAddress"],
                        "signatureType": 0,
                    },
                }
                floor_key = "polygon_nonce_floor:" + payload["publicAddress"].lower()
                with closing(sqlite3.connect(tmp_path / "LIVE/state.sqlite")) as db, db:
                    db.execute(
                        "INSERT INTO meta(key,value) VALUES(?,?)",
                        (APPROVAL_KEY, json.dumps(record)),
                    )
                    db.execute("INSERT INTO meta(key,value) VALUES(?,?)", (floor_key, "12"))
                removed = await client.post(
                    "/api/live/wallet/remove",
                    headers=headers,
                    json={
                        "confirmation": "REPLACE EMPTY WALLET",
                        "vaultPassword": payload["vaultPassword"],
                    },
                )
                assert removed.status_code == 200 and removed.json()["configured"] is False
                new_key = "0x" + "2" * 64
                new_address = Account.from_key(new_key).address
                imported = await client.post(
                    "/api/live/wallet/import",
                    headers=headers,
                    json=payload | {"secret": new_key, "publicAddress": new_address},
                )
                assert imported.status_code == 200 and imported.json()["signer"] == new_address
                with closing(sqlite3.connect(tmp_path / "LIVE/state.sqlite")) as db:
                    assert (
                        db.execute(
                            "SELECT value FROM meta WHERE key=?", (APPROVAL_KEY,)
                        ).fetchone()[0]
                        == "null"
                    )
                    assert (
                        db.execute("SELECT value FROM meta WHERE key=?", (floor_key,)).fetchone()[0]
                        == "12"
                    )

    asyncio.run(run())


def test_shutdown_retains_process_lock_until_cancelled_backup_finishes(tmp_path, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr("pm_nautilus.live_web_setup.build_live_settings", _settings)
    entered, release = Event(), Event()
    encrypt = backup._encrypt_file

    def gated_encrypt(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return encrypt(*args, **kwargs)

    monkeypatch.setattr(backup, "_encrypt_file", gated_encrypt)

    async def run():
        app = app_module.create_app(tmp_path, public_data=False)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        test_store = app.state.runtimes["TEST"].store
        task = shutdown = None
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                headers = {"x-pm-csrf": (await client.get("/api/dashboard")).headers["x-pm-csrf"]}
                assert (
                    await client.post("/api/live/wallet/import", json=_payload(), headers=headers)
                ).status_code == 200
                task = asyncio.create_task(
                    endpoint(app, "/api/live/wallet/backup")(
                        request({"vaultPassword": _payload()["vaultPassword"]})
                    )
                )
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel()
                await asyncio.sleep(0)
                shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
                for _ in range(10):
                    await asyncio.sleep(0)
                shutdown.cancel()
                await asyncio.sleep(0)
                shutdown.cancel()
                await asyncio.sleep(0)
                assert not task.done() and not shutdown.done() and not test_store._closed
                with (tmp_path / "process.lock").open("a") as competing:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
                blocked = await client.post("/api/live/wallet/check", json={}, headers=headers)
                assert blocked.status_code == 503
                assert list((tmp_path / "LIVE").glob(".pm-live-export-*"))
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(shutdown, 5)
                assert test_store._closed
                assert not list((tmp_path / "LIVE").glob(".pm-live-export-*"))
                with (tmp_path / "process.lock").open("a") as competing:
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            release.set()
            await asyncio.gather(*(x for x in (task, shutdown) if x), return_exceptions=True)
            if shutdown is None:
                await lifespan.__aexit__(None, None, None)

    asyncio.run(run())


def test_restore_empty_guard_rejects_unbound_pending_authorization(tmp_path):
    store = Store(tmp_path / "LIVE/state.sqlite", "LIVE")
    try:
        assert backup._empty(store)
        store.put(APPROVAL_KEY, {"status": "ACTIVE", "pending": None})
        assert not backup._empty(store)
        store.put(APPROVAL_KEY, {"status": "FAILED", "pending": {"raw_tx": "0x00"}})
        assert not backup._empty(store)
    finally:
        store.close()


def test_startup_cleanup_removes_only_private_dedicated_scratch_under_process_lock(
    tmp_path, monkeypatch
):
    configure(monkeypatch)
    live = tmp_path / "LIVE"
    live.mkdir()
    abandoned = Path(tempfile.mkdtemp(prefix=".pm-live-export-", dir=live))
    (abandoned / "bundle.pmnb").write_bytes(b"encrypted marker")
    restore = Path(tempfile.mkdtemp(prefix=".pm-live-restore-", dir=live))
    (restore / "restored.sqlite").write_bytes(b"private snapshot marker")
    fd, name = tempfile.mkstemp(prefix=".pm-live-upload-", suffix=".pmnb", dir=live)
    os.close(fd)
    upload = Path(name)
    user_folder = live / ".pm-live-export-user-files"
    user_folder.mkdir(mode=0o700)
    (user_folder / "important.txt").write_text("keep")
    unrecognized = Path(tempfile.mkdtemp(prefix=".pm-live-export-", dir=live))
    (unrecognized / "important.txt").write_text("keep")
    shared = Path(tempfile.mkdtemp(prefix=".pm-live-export-", dir=live))
    shared.chmod(0o755)
    external = tmp_path / "outside"
    external.mkdir()
    (external / "bundle.pmnb").write_text("keep")
    link = live / ".pm-live-export-abcd1234"
    link.symlink_to(external, target_is_directory=True)

    async def run():
        app = app_module.create_app(tmp_path, public_data=False)
        async with app.router.lifespan_context(app):
            assert not abandoned.exists() and not restore.exists() and not upload.exists()
            assert user_folder.exists() and unrecognized.exists() and shared.exists()
            assert link.is_symlink() and (external / "bundle.pmnb").is_file()

    asyncio.run(run())


def test_final_export_and_secret_bundle_sync_directory_after_atomic_publish(tmp_path, monkeypatch):
    root = tmp_path / "data"
    store = Store(root / "LIVE/state.sqlite", "LIVE")
    vault = root / "wallet/live.vault"
    store.put("live_wallet_identity", write_vault(vault, backup_settings(), PASSWORD))
    store.close()
    sync = os.fsync
    operations = []

    def observed_sync(fd):
        operations.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return sync(fd)

    monkeypatch.setattr(os, "fsync", observed_sync)
    destination = tmp_path / "final.pmnb"
    assert len(backup.export_final_live_bundle_file(root, PASSWORD, destination)) == 64
    assert operations[-2:] == ["file", "directory"]
    assert destination.is_file()
    operations.clear()
    module = bundle_module()
    secret = tmp_path / "credentials.age"
    module.write_new_file(secret, b"encrypted marker", os.geteuid(), os.getegid())
    assert operations == ["file", "directory"]
    assert secret.read_bytes() == b"encrypted marker"
