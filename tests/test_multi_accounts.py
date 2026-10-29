"""Ten-account integration and preservation checks using local ledgers only."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import Mock

import httpx
import pytest

import pm_nautilus.app as app_module
from pm_nautilus.app import create_app
from pm_nautilus.market import MarketService, PublicMarketHub
from pm_nautilus.performance import PerformanceSampler
from pm_nautilus.store import Store
from pm_nautilus.strategy import Runtime

from test_market_efficiency import IdleSocket, public_book, settle_tasks
from test_runtime import setup


RESET = {"confirmation": "RESET TEST", "finalConfirmation": "RESET TEST AGAIN"}


@pytest.fixture(autouse=True)
def local_test_only(monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("PM_UI_PASSWORD", raising=False)


@asynccontextmanager
async def account_app(path, clock=None, public_data=False):
    app = create_app(path, public_data=public_data, test_clock=clock)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/api/dashboard")
            assert response.status_code == 200
            client.headers["x-pm-csrf"] = response.headers["x-pm-csrf"]
            yield app, client


def financial_snapshot(store):
    result = {
        table: list(store.db.execute(f"SELECT * FROM {table} ORDER BY 1,2"))
        for table in ("native_events", "intents", "tokens", "books", "business_parts")
    }
    result["meta"] = {
        key: store.get(key) for key in ("preferences", "initial_capital", "business", "generation")
    }
    # sqlite3.Row equality can depend on field descriptions; compare actual values.
    return {
        key: [tuple(row) for row in value] if isinstance(value, list) else value
        for key, value in result.items()
    }


def test_existing_account_and_live_ledger_survive_first_multi_account_boot(tmp_path):
    source, clock, token = setup(tmp_path / "source")
    source.close()
    old = Runtime(tmp_path / "TEST" / "state.sqlite", clock=clock)
    old.update_preferences({"orderAmount": "2", "stopLossEnabled": False}, capital="321")
    old.add_tokens([token])
    old.book("1", [(15_000, 100_000_000)], [(20_000, 100_000_000)])
    old.start()
    old.pause()
    sampler = PerformanceSampler(old)
    sampler.sample(at_ms=1_800_000)
    expected = financial_snapshot(old.store)
    equity = [tuple(row) for row in old.store.db.execute("SELECT * FROM equity_samples")]
    assert expected["intents"] and expected["native_events"] and equity
    old.close()
    live = Store(tmp_path / "LIVE" / "state.sqlite", "LIVE")
    live.put("preferences", live.get("preferences") | {"orderAmount": "7"})
    live_expected = financial_snapshot(live)
    live.close()

    async def run():
        async with account_app(tmp_path, clock) as (app, client):
            runtimes = app.state.test_runtimes
            assert set(runtimes) == set(range(1, 11))
            assert len({id(runtime.native) for runtime in runtimes.values()}) == 10
            assert len({runtime.store.path.resolve() for runtime in runtimes.values()}) == 10
            first = runtimes[1]
            assert first is app.state.runtimes["TEST"]
            assert first.store.path == tmp_path / "TEST" / "state.sqlite"
            assert financial_snapshot(first.store) == expected
            assert [
                tuple(row)
                for row in first.store.db.execute(
                    "SELECT * FROM equity_samples WHERE id<=?", (equity[-1][0],)
                )
            ] == equity
            for number in range(2, 11):
                runtime = runtimes[number]
                assert runtime.status == "PAUSED"
                assert runtime.store.get("preferences") == expected["meta"]["preferences"]
                assert runtime.store.get("initial_capital") == 321_000_000
                assert not runtime.store.intents() and not runtime.store.events()
                assert not runtime.business["cycles"] and not runtime.business["targets"]
                assert runtime.cash() == 321_000_000
                assert runtime.store.generation != first.store.generation
            assert (await client.get("/api/dashboard?mode=LIVE")).json()["preferences"][
                "orderAmount"
            ] == "7"

    asyncio.run(run())
    live = Store(tmp_path / "LIVE" / "state.sqlite", "LIVE")
    assert financial_snapshot(live) == live_expected
    live.close()


def test_account_parameters_capital_controls_and_queries_are_isolated(tmp_path):
    async def run():
        async with account_app(tmp_path) as (app, client):
            original = app.state.test_runtimes[1].store.get("preferences")
            response = await client.put(
                "/api/TEST/preferences?account_id=2",
                json={"orderAmount": "3", "initialCapital": "456", "minBuyPriceCents": "3"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["accountId"] == 2
            assert app.state.test_runtimes[1].store.get("preferences") == original
            assert app.state.test_runtimes[1].cash() == 100_000_000
            assert app.state.test_runtimes[2].cash() == 456_000_000
            for number in range(1, 11):
                dashboard = await client.get(f"/api/dashboard?account_id={number}")
                assert dashboard.status_code == 200
                assert dashboard.json()["accountId"] == number
                for endpoint in ("validation", "performance", "trade-records"):
                    result = await client.get(f"/api/TEST/{endpoint}?account_id={number}")
                    assert result.status_code == 200, result.text
                    assert result.json()["accountId"] == number
            for invalid in ("0", "11", "-1", "1.5", "other"):
                assert (await client.get(f"/api/dashboard?account_id={invalid}")).status_code in {
                    400,
                    422,
                }
            for endpoint in (
                "preferences",
                "start",
                "pause",
                "reset",
                "validation",
                "performance",
                "trade-records",
            ):
                path = f"/api/LIVE/{endpoint}?account_id=2"
                method = (
                    "put"
                    if endpoint == "preferences"
                    else ("post" if endpoint in {"start", "pause", "reset"} else "get")
                )
                options = {"json": RESET if endpoint == "reset" else {}} if method != "get" else {}
                assert (await getattr(client, method)(path, **options)).status_code == 400

    asyncio.run(run())


def test_ten_running_accounts_share_market_without_consuming_each_others_depth(tmp_path):
    reference, clock, token = setup(tmp_path / "reference")
    reference.close()

    async def run():
        async with account_app(tmp_path, clock) as (app, client):
            for number, runtime in app.state.test_runtimes.items():
                runtime.add_tokens([token])
                runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
                response = await client.post(f"/api/TEST/start?account_id={number}")
                assert response.status_code == 200, response.text
            assert all(r.status == "RUNNING" for r in app.state.test_runtimes.values())
            assert len({id(r.books["1"]) for r in app.state.test_runtimes.values()}) == 10
            for runtime in app.state.test_runtimes.values():
                assert runtime.business["cycles"]["event"]["quantity"] == 50_000_000
                assert runtime.cash() == 99_000_000
                assert runtime.books["1"].ask.consumed == {20_000: 50_000_000}
                assert runtime.validate()["ok"]
            page = (
                await client.get("/api/TEST/trade-records?account_id=1&paged=true&limit=1")
            ).json()
            assert page["records"]
            assert (
                await client.get(
                    "/api/TEST/trade-records",
                    params={
                        "account_id": 2,
                        "paged": "true",
                        "cursor": page["nextCursor"],
                    },
                )
            ).status_code == 400
            await client.post("/api/TEST/pause?account_id=2")
            assert app.state.test_runtimes[2].status == "PAUSED"
            assert all(r.status == "RUNNING" for n, r in app.state.test_runtimes.items() if n != 2)
            before = {n: financial_snapshot(r.store) for n, r in app.state.test_runtimes.items()}
            assert (
                await client.post("/api/TEST/reset?account_id=3", json=RESET)
            ).status_code == 400
            response = await client.post("/api/TEST/reset?account_id=2", json=RESET)
            assert response.status_code == 200, response.text
            assert response.json()["accountId"] == 2
            assert not app.state.test_runtimes[2].store.intents()
            assert app.state.test_runtimes[2].cash() == 100_000_000
            assert app.state.test_runtimes[2].store.generation != before[2]["meta"]["generation"]
            for number, runtime in app.state.test_runtimes.items():
                if number != 2:
                    assert financial_snapshot(runtime.store) == before[number]
                    assert runtime.status == "RUNNING"

    asyncio.run(run())


def test_restart_restores_each_account_ledger_and_parameters_but_pauses_all(tmp_path):
    reference, clock, token = setup(tmp_path / "reference")
    reference.close()
    expected = {}

    async def run():
        async with account_app(tmp_path, clock) as (app, client):
            for number, runtime in app.state.test_runtimes.items():
                runtime.update_preferences({"orderAmount": str(number)}, capital=str(100 + number))
                runtime.add_tokens([token])
                runtime.book("1", [(15_000, 100_000_000)], [(20_000, 500_000_000)])
                runtime.start()
                expected[number] = financial_snapshot(runtime.store)
        async with account_app(tmp_path, clock) as (app, client):
            for number, runtime in app.state.test_runtimes.items():
                assert runtime.status == "PAUSED"
                assert financial_snapshot(runtime.store) == expected[number]
                assert runtime.validate()["ok"]
                data = (await client.get(f"/api/dashboard?account_id={number}")).json()
                assert data["preferences"]["orderAmount"] == str(number)
                assert data["strategy"]["initialCapital"] == str(100 + number)

    asyncio.run(run())


def test_ten_consumer_fault_and_reset_leave_other_shared_subscriptions_alive(tmp_path, monkeypatch):
    async def run():
        sockets = []

        def connect(*args, **kwargs):
            socket = IdleSocket()
            sockets.append(socket)
            return socket

        monkeypatch.setattr("pm_nautilus.market.connect", connect)
        runtimes, services = [], []
        hub = PublicMarketHub()
        try:
            for number in range(1, 11):
                runtime, _, token = setup(tmp_path / str(number))
                runtimes.append(runtime)
                services.append(MarketService(runtime, hub=hub))
            await services[0].sync_subscriptions()
            await settle_tasks()
            hub.message(public_book(token), {"1"})
            for runtime in runtimes:
                runtime.start()
            assert len(sockets) == 1
            failed = services[4]
            failed.message = Mock(side_effect=ValueError("controlled account 5 failure"))
            hub.message(public_book(token, timestamp=1001), {"1"})
            assert runtimes[4].status == "PAUSED"
            assert failed.scan_status["serviceError"] == "ValueError"
            assert all(runtime.status == "RUNNING" for n, runtime in enumerate(runtimes) if n != 4)
            group = next(iter(hub.tasks))
            shared_task = hub.tasks[group]
            old = runtimes[1]
            old.pause()
            await services[1].close()
            old.store.reset()
            old.close()
            replacement, _, _ = setup(tmp_path / "2")
            runtimes[1] = replacement
            services[1] = MarketService(replacement, hub=hub)
            await services[1].sync_subscriptions()
            assert hub.tasks[group] is shared_task and not shared_task.done()
            assert len(sockets) == 1
            assert replacement.books["1"].ready
            assert replacement.books["1"].ask.consumed == {}
            for n, runtime in enumerate(runtimes):
                if n not in {1, 4}:
                    assert runtime.status == "RUNNING"
                    assert runtime.books["1"].ask.consumed == {20_000: 50_000_000}
        finally:
            for service in services:
                await service.close()
            await hub.close()
            for runtime in runtimes:
                runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("cancel_reader", [False, True])
def test_validation_and_reset_serialize_only_their_own_account(
    tmp_path, monkeypatch, cancel_reader
):
    async def run():
        async with account_app(tmp_path) as (app, client):
            old = app.state.test_runtimes[2]
            generation = old.store.generation
            entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
            validate = old.validate_async

            async def delayed_validation():
                entered.set()
                await release.wait()
                assert not old.store._closed
                result = await validate()
                finished.set()
                return result

            monkeypatch.setattr(old, "validate_async", delayed_validation)
            reader = asyncio.create_task(client.get("/api/TEST/validation?account_id=2"))
            await entered.wait()
            if cancel_reader:
                reader.cancel()
                await asyncio.sleep(0)
                assert not reader.done()
            reset = asyncio.create_task(client.post("/api/TEST/reset?account_id=2", json=RESET))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not reset.done()
            third = await asyncio.wait_for(client.get("/api/dashboard?account_id=3"), timeout=5)
            assert third.status_code == 200 and third.json()["accountId"] == 3
            assert not old.store._closed and old.store.generation == generation
            release.set()
            if cancel_reader:
                with pytest.raises(asyncio.CancelledError):
                    await reader
            else:
                assert (await reader).status_code == 200
            assert (await reset).status_code == 200
            assert finished.is_set() and old.store._closed
            assert app.state.test_runtimes[2].store.generation != generation

    asyncio.run(run())


def test_failed_account_reset_is_quarantined_without_stopping_other_accounts(tmp_path, monkeypatch):
    async def run():
        async with account_app(tmp_path) as (app, client):
            old = app.state.test_runtimes[2]
            unaffected = {
                n: financial_snapshot(r.store) for n, r in app.state.test_runtimes.items() if n != 2
            }

            def fail_reset(path, *args, **kwargs):
                if path == old.store.path:
                    raise ValueError("controlled account 2 reconstruction failure")
                return Runtime(path, *args, **kwargs)

            monkeypatch.setattr(app_module, "Runtime", fail_reset)
            result = await client.post("/api/TEST/reset?account_id=2", json=RESET)
            assert result.status_code == 400
            assert old.store._closed
            assert app.state.test_services[2].closed
            health = await client.get("/api/health")
            assert health.status_code == 200
            assert health.json()["status"] == "attention"
            assert health.json()["accountErrors"] == {"TEST:2": "test_account_unavailable"}
            assert health.json()["backgroundErrors"] == {"TEST:2": "test_account_unavailable"}
            assert (await client.get("/api/dashboard?account_id=2")).status_code == 503
            for number, snapshot in unaffected.items():
                result = await client.get(f"/api/dashboard?account_id={number}")
                assert result.status_code == 200
                assert financial_snapshot(app.state.test_runtimes[number].store) == snapshot

    asyncio.run(run())


def test_synthetic_shared_feed_ten_account_load_and_http_responsiveness(tmp_path, monkeypatch):
    """Use production message handling; print measured, non-production capacity evidence."""
    reference, clock, token = setup(tmp_path / "reference")
    reference.close()

    async def idle_service(self):
        await asyncio.Future()

    monkeypatch.setattr(MarketService, "run", idle_service)
    monkeypatch.setattr("pm_nautilus.market.connect", lambda *a, **k: IdleSocket())

    async def run():
        async with account_app(tmp_path, clock, public_data=True) as (app, client):
            hub = app.state.market_hub
            tokens = [
                replace(
                    token,
                    token_id=str(n),
                    event_id=f"event-{n}",
                    market_id=f"market-{n}",
                    condition_id=f"condition-{n}",
                )
                for n in range(1, 21)
            ]
            for runtime in app.state.test_runtimes.values():
                runtime.add_tokens(tokens)
            await app.state.test_services[1].sync_subscriptions()
            await settle_tasks()
            allowed = {item.token_id for item in tokens}
            for item in tokens:
                hub.message(public_book(item), allowed)
            for runtime in app.state.test_runtimes.values():
                runtime.start()
            durations, responses = [], []
            started = time.perf_counter()

            async def stream():
                for count in range(1000):
                    item = tokens[count % len(tokens)]
                    msg = public_book(item, timestamp=1001 + count)
                    tick = time.perf_counter()
                    hub.message(msg, allowed)
                    durations.append(time.perf_counter() - tick)
                    await asyncio.sleep(0)

            async def read_dashboard():
                for count in range(50):
                    tick = time.perf_counter()
                    response = await client.get(
                        f"/api/dashboard?account_id={count % 10 + 1}&details=summary"
                    )
                    responses.append(time.perf_counter() - tick)
                    assert response.status_code == 200, response.text
                    await asyncio.sleep(0)

            await asyncio.gather(stream(), read_dashboard())
            elapsed = time.perf_counter() - started
            for runtime in app.state.test_runtimes.values():
                assert runtime.status == "RUNNING"
                assert runtime.validate()["ok"]
            metrics = {
                "accounts": 10,
                "markets": 20,
                "messages": len(durations),
                "consumer_deliveries": len(durations) * 10,
                "feed_seconds": round(elapsed, 4),
                "messages_per_second": round(len(durations) / elapsed, 1),
                "feed_p95_ms": round(sorted(durations)[int(len(durations) * 0.95)] * 1000, 3),
                "dashboard_p95_ms": round(sorted(responses)[int(len(responses) * 0.95)] * 1000, 3),
                "dashboard_max_ms": round(max(responses) * 1000, 3),
            }
            print("multi_account_load=" + json.dumps(metrics, sort_keys=True))
            # Wide regression guard, not an operational server latency promise.
            assert max(responses) < 10
            assert elapsed < 60

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["construct", "attach"])
def test_account_one_startup_failure_does_not_disable_existing_siblings(
    tmp_path, monkeypatch, stage
):
    from fastapi.testclient import TestClient

    # Existing sibling settings cannot depend on a broken primary account.
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        headers = {"x-pm-csrf": client.get("/api/dashboard").headers["x-pm-csrf"]}
        assert (
            client.put(
                "/api/TEST/preferences?account_id=2",
                json={"initialCapital": "234", "orderAmount": "2"},
                headers=headers,
            ).status_code
            == 200
        )
    original_runtime = app_module.Runtime
    original_sampler = app_module.PerformanceSampler
    primary = tmp_path / "TEST" / "state.sqlite"

    def runtime(path, *args, **kwargs):
        if stage == "construct" and path == primary:
            raise ValueError("controlled primary constructor failure")
        return original_runtime(path, *args, **kwargs)

    def sampler(runtime):
        if stage == "attach" and runtime.store.path == primary:
            raise ValueError("controlled primary sampler failure")
        return original_sampler(runtime)

    monkeypatch.setattr(app_module, "Runtime", runtime)
    monkeypatch.setattr(app_module, "PerformanceSampler", sampler)
    app = create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "attention"
        assert health.json()["availableTestAccountCount"] == 9
        assert "TEST" in health.json()["accountErrors"]
        assert client.get("/api/dashboard?account_id=1").status_code == 503
        response = client.get("/api/dashboard?account_id=2")
        assert response.status_code == 200
        assert response.json()["strategy"]["initialCapital"] == "234"
        assert response.json()["preferences"]["orderAmount"] == "2"
        headers = {"x-pm-csrf": response.headers["x-pm-csrf"]}
        started = client.post("/api/TEST/start?account_id=2", headers=headers)
        assert started.status_code == 200
        assert started.json()["strategy"]["status"] == "RUNNING"
        assert client.get("/api/dashboard?account_id=3").json()["strategy"]["status"] == "PAUSED"
        assert client.get("/api/dashboard?mode=LIVE").status_code == 200
