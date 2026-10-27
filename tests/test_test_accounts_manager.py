"""Account initialization preserves the legacy ledger and never reseeds an account."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from pm_nautilus.app import create_app
import pm_nautilus.app as app_module
from pm_nautilus.market import MarketService
from pm_nautilus.performance import PerformanceSampler
from pm_nautilus.store import Store
from pm_nautilus.strategy import Runtime
from pm_nautilus.test_accounts import TestAccounts

from test_lifecycle import controlled_factory


def test_paths_and_strict_account_ids(tmp_path):
    accounts = TestAccounts(tmp_path)
    assert len(accounts.contexts) == 10
    assert accounts.path(1) == tmp_path / "TEST" / "state.sqlite"
    assert accounts.path(10) == tmp_path / "TEST" / "accounts" / "10" / "state.sqlite"
    assert accounts.account_id() == 1
    assert accounts.account_id("2") == 2
    for invalid in (0, 11, True, "01", "../1", "1.0", "", "１"):
        with pytest.raises(ValueError, match="1–10"):
            accounts.account_id(invalid)


def test_new_account_copies_only_settings_and_is_not_reseeded(tmp_path):
    accounts = TestAccounts(tmp_path)
    source = Store(accounts.path(1))
    try:
        generation = source.generation
        preferences = source.get("preferences") | {"maxMarketProgressPercent": 70}
        source.put("preferences", preferences)
        source.put("initial_capital", 350_000_000)
        source.put("private_test_marker", "must remain only in account one")
        source.put("status", "RUNNING")
        accounts.prepare(1, source)
        assert source.generation == generation
        assert source.get("status") == "RUNNING"
        path = accounts.prepare(2, source)
        account = Store(path)
        try:
            assert account.get("preferences") == preferences
            assert account.get("initial_capital") == 350_000_000
            assert account.get("private_test_marker") is None
            assert account.get("business") == Store.empty_business()
            assert account.tokens() == {}
            assert account.intents() == {}
            assert account.events() == []
            assert account.get("status") == "PAUSED"
            own_generation = account.generation
            assert own_generation != generation
            own_preferences = preferences | {"maxMarketProgressPercent": 65}
            account.put("preferences", own_preferences)
            account.put("initial_capital", 250_000_000)
        finally:
            account.close()
        source.put("initial_capital", 450_000_000)
        accounts.prepare(2, source)
        reopened = Store(path)
        try:
            assert reopened.generation == own_generation
            assert reopened.get("preferences") == own_preferences
            assert reopened.get("initial_capital") == 250_000_000
        finally:
            reopened.close()
    finally:
        source.close()


def test_context_lock_survives_runtime_replacement_and_quiesce(tmp_path):
    async def run():
        accounts = TestAccounts(tmp_path)
        context = accounts.attach_runtime(2, SimpleNamespace(version=1))
        original_lock = context.lock
        replacement = accounts.attach_runtime(2, SimpleNamespace(version=2))
        assert replacement is context and replacement.lock is original_lock
        assert accounts.runtimes[2].version == 2
        assert len({id(account.lock) for account in accounts.contexts.values()}) == 10
        async with accounts.quiesce():
            assert all(account.lock.locked() for account in accounts.contexts.values())
        assert not any(account.lock.locked() for account in accounts.contexts.values())

    asyncio.run(run())


def test_reset_account_keeps_its_new_settings_after_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    for boot in range(2):
        with TestClient(create_app(tmp_path, public_data=False)) as client:
            first = client.get("/api/dashboard?account_id=1")
            csrf = {"x-pm-csrf": first.headers["x-pm-csrf"]}
            if boot == 0:
                original_generation = first.json()["generation"]
                response = client.post(
                    "/api/test/reset?account_id=2",
                    json={"confirmation": "RESET TEST", "finalConfirmation": "RESET TEST AGAIN"},
                    headers=csrf,
                )
                assert response.status_code == 200
                reset_generation = response.json()["generation"]
                updated = client.put(
                    "/api/test/preferences?account_id=2",
                    json={"initialCapital": "225", "orderAmount": "2"},
                    headers=csrf,
                )
                assert updated.status_code == 200
                assert updated.json()["capitalEditable"] is True
            else:
                assert first.json()["generation"] == original_generation
                account = client.get("/api/dashboard?account_id=2").json()
                assert account["generation"] == reset_generation
                assert account["strategy"]["initialCapital"] == "225"
                assert account["preferences"]["orderAmount"] == "2"
                assert account["strategy"]["status"] == "PAUSED"
                assert account["capitalEditable"] is True


@pytest.mark.parametrize("evidence", ["native", "business", "equity", "settings", "capital"])
def test_unmarked_existing_financial_data_is_never_seeded(tmp_path, evidence):
    accounts = TestAccounts(tmp_path)
    source = Store(accounts.path(1))
    account = Store(accounts.path(2))
    generation = account.generation
    try:
        source.put("initial_capital", 450_000_000)
        if evidence == "native":
            account.db.execute(
                "INSERT INTO native_events(event_id,kind,payload) VALUES('existing','OrderFilled','{}')"
            )
        elif evidence == "business":
            account.put("business", account.get("business") | {"realized": 10_000_000})
        elif evidence == "equity":
            account.db.execute("CREATE TABLE equity_samples(id INTEGER)")
            account.db.execute("INSERT INTO equity_samples VALUES(1)")
        elif evidence == "settings":
            account.put("preferences", account.get("preferences") | {"orderAmount": "3"})
        else:
            account.put("initial_capital", 225_000_000)
        account.close()
        with pytest.raises(ValueError, match="不能覆盖初始化"):
            accounts.prepare(2, source)
        account = Store(accounts.path(2))
        assert account.generation == generation
        assert account.get("initial_capital") != 450_000_000
        assert account.get("test_account_initialized") is None
    finally:
        account.close()
        source.close()


@pytest.mark.parametrize("failure", ["corrupt", "runtime", "attach"])
def test_additional_account_boot_failure_keeps_other_accounts_and_live(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setenv("PM_LIVE_ENABLED", "true")
    monkeypatch.setenv("PM_UI_PASSWORD", "controlled-local-password")
    monkeypatch.setattr("pm_nautilus.live.load_settings", lambda: {})
    monkeypatch.setattr("pm_nautilus.live.live_factory", lambda *_: controlled_factory)
    monkeypatch.setattr(
        "pm_nautilus.redemption.PolygonWallet",
        lambda _: SimpleNamespace(preflight=lambda: None),
    )
    created_runtimes, created_services = [], []

    async def idle(_self=None):
        await asyncio.Future()

    def runtime(path, *args, **kwargs):
        if failure == "runtime" and path.parent.name == "2":
            raise RuntimeError("controlled account two runtime failure")
        result = Runtime(path, *args, **kwargs)
        created_runtimes.append(result)
        if result.mode == "LIVE":

            async def activate():
                result.client.ready = result.client.open_orders_clear = True
                result.client.reconciliation_task = asyncio.create_task(idle())

            result.client.activate = AsyncMock(side_effect=activate)
        return result

    def service(owner, hub=None):
        result = MarketService(owner, hub=hub)
        created_services.append(result)
        return result

    def sampler(owner):
        if failure == "attach" and owner.store.path.parent.name == "2":
            raise RuntimeError("controlled account two attachment failure")
        return PerformanceSampler(owner)

    monkeypatch.setattr(app_module, "Runtime", runtime)
    monkeypatch.setattr(app_module, "MarketService", service)
    monkeypatch.setattr(app_module, "PerformanceSampler", sampler)
    monkeypatch.setattr(MarketService, "run", idle)
    if failure == "corrupt":
        broken = tmp_path / "TEST" / "accounts" / "2" / "state.sqlite"
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"controlled invalid SQLite file")

    async def run():
        app = create_app(tmp_path, public_data=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                auth=("pm", "controlled-local-password"),
            ) as client:
                for number in (1, *range(3, 11)):
                    result = await client.get(f"/api/dashboard?account_id={number}")
                    assert result.status_code == 200
                    assert result.json()["accountId"] == number
                assert (await client.get("/api/dashboard?account_id=2")).status_code == 503
                assert (await client.get("/api/dashboard?mode=LIVE")).status_code == 200
                assert (await client.get("/api/health")).json()["backgroundErrors"] == {
                    "TEST:2": "test_account_unavailable"
                }
                assert all(
                    consumer.runtime.store.path.parent.name != "2"
                    for consumer in app.state.market_hub.services
                )
        assert all(runtime.store._closed for runtime in created_runtimes)
        assert all(service.closed and service.loop_task.done() for service in created_services)
        assert not app.state.market_hub.services
        live = next(runtime for runtime in created_runtimes if runtime.mode == "LIVE")
        assert live.native.execution.get_cmd_queue_task().done()
        assert live.native.execution.get_evt_queue_task().done()

    asyncio.run(run())
