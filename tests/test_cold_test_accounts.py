"""Cold TEST accounts retain their own persisted view and any maintenance rights."""

import asyncio
import json
import os
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from pm_nautilus.store import Store
from pm_nautilus.strategy import Runtime
from pm_nautilus.test_accounts import TestAccounts
from pm_nautilus.views import records, records_page_async
from test_runtime import setup


def test_uncreated_account_stays_uncreated(tmp_path):
    accounts = TestAccounts(tmp_path)
    assert accounts.cold_eligible(2)
    with accounts.open_cold(2) as store:
        assert store is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "state", ["ledger-link", "directory-link", "hard-link", "orphan-wal", "invalid-file"]
)
def test_uncertain_path_is_not_an_uncreated_account(tmp_path, state):
    accounts = TestAccounts(tmp_path)
    path = accounts.path(2)
    if state == "directory-link":
        path.parent.parent.mkdir(parents=True)
        path.parent.symlink_to(tmp_path / "missing")
    else:
        path.parent.mkdir(parents=True)
        if state == "ledger-link":
            path.symlink_to(tmp_path / "missing")
        elif state == "hard-link":
            with closing(Store(accounts.path(1))):
                pass
            os.link(accounts.path(1), path)
        elif state == "orphan-wal":
            path.with_name(path.name + "-wal").touch()
        else:
            path.write_bytes(b"not a database")
    assert not accounts.cold_eligible(2)
    if state != "invalid-file":
        with pytest.raises(ValueError, match="无效"):
            accounts.check_ledger_path(2)


@pytest.mark.parametrize(
    "state",
    [
        "live",
        "running",
        "cycle",
        "target",
        "uncredited",
        "order",
        "unknown-order",
        "gap",
        "recovery-error",
        "unknown-business",
    ],
)
def test_unfinished_or_unknown_work_keeps_account_warm(tmp_path, state):
    accounts = TestAccounts(tmp_path)
    with closing(Store(accounts.path(2), "LIVE" if state == "live" else "TEST")) as writer:
        business = writer.get("business")
        if state == "running":
            writer.put("status", "RUNNING")
        elif state == "cycle":
            business["cycles"]["event"] = {"token_id": "1", "quantity": 1}
        elif state == "target":
            business["targets"]["target"] = {"event_id": "event", "qty": 1}
        elif state == "uncredited":
            business["claims"]["condition"] = {"state": "IDENTIFIED"}
        elif state in {"order", "unknown-order"}:
            intent = {"event_id": "event", "token_id": "1", "side": "SELL"}
            if state == "order":
                intent["terminal"] = False
            writer.save_intent("pending", intent)
        elif state == "gap":
            writer.db.execute(
                "INSERT INTO native_events(event_id,kind,payload) VALUES('pending','OrderFilled','{}')"
            )
        elif state == "recovery-error":
            business["recovery_error"] = "recover projection"
        elif state == "unknown-business":
            writer.db.execute("INSERT INTO business_parts VALUES('future','unknown','{}')")
        if state in {"cycle", "target", "uncredited", "recovery-error"}:
            writer.put("business", business)
        assert not accounts.cold_eligible(2)


@pytest.mark.parametrize("legacy", [False, True])
def test_credited_settlement_can_be_read_cold_without_migration(tmp_path, legacy):
    accounts = TestAccounts(tmp_path)
    path = accounts.path(2)
    with closing(Store(path)) as writer:
        business = writer.get("business")
        business["claims"]["condition"] = {"state": "CREDITED"}
        if legacy:
            writer.db.execute("DROP TABLE business_parts")
            writer.db.execute(
                "UPDATE meta SET value=? WHERE key='business'", (json.dumps(business),)
            )
        else:
            writer.put("business", business)
    before = path.read_bytes()
    assert accounts.cold_eligible(2)
    with accounts.open_cold(2) as store:
        assert store.get("business") == business
    assert path.read_bytes() == before


def test_read_only_guard_observes_work_committed_to_wal(tmp_path):
    accounts = TestAccounts(tmp_path)
    with closing(Store(accounts.path(2))) as writer:
        writer.db.execute("PRAGMA wal_autocheckpoint=0")
        writer.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert accounts.cold_eligible(2)
        business = writer.get("business")
        business["claims"]["condition"] = {"state": "CONFIRMED"}
        writer.put("business", business)
        assert writer.path.with_name(writer.path.name + "-wal").stat().st_size > 0
        assert not accounts.cold_eligible(2)


def test_read_only_view_pages_existing_fills_and_reuses_cash_calculation(tmp_path):
    accounts = TestAccounts(tmp_path)
    path = accounts.path(2)
    runtime, _, token = setup(path.parent)
    try:
        runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        runtime.start()
        runtime.pause()
        runtime.book("1", [(40_000, 100_000_000)], [(20_000, 50_000_000)])
        expected = records(runtime, 2)["records"]
        cash = runtime.test_cash()
        generation = runtime.store.generation
        assert [record["type"] for record in expected] == ["CLOSE", "OPEN"]
    finally:
        runtime.close()
    runtime.store.path.rename(path)
    before = path.read_bytes()

    assert accounts.cold_eligible(2)
    with accounts.open_cold(2) as store:
        assert store.path == path.resolve() and store.mode == "TEST"
        assert store.generation == generation and store.has_history()
        assert store.token("1") == token
        with pytest.raises(KeyError):
            store.token("absent")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.db.execute("UPDATE meta SET value='null' WHERE key='status'")
        view = SimpleNamespace(store=store, tokens=store.history_tokens)
        assert Runtime.test_cash(view) == cash

        async def pages():
            first = await records_page_async(view, limit=1)
            second = await records_page_async(view, limit=1, cursor=first["nextCursor"])
            return first["records"] + second["records"]

        rows = asyncio.run(pages())
        assert [{k: v for k, v in row.items() if k != "firstSeq"} for row in rows] == expected
    assert store._closed
    assert path.read_bytes() == before
