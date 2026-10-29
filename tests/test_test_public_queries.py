"""Concurrent public TEST observations remain fresh, independent and bounded."""

import asyncio

import pytest

from pm_nautilus.market import CLOB_BOOK, GAMMA, MarketService
from pm_nautilus.test_public_queries import TestPublicQueries


def test_ten_concurrent_requests_share_only_pending_reply_and_clone_json():
    async def run():
        broker = TestPublicQueries()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def fetch(url, **params):
            calls.append((url, params))
            entered.set()
            await release.wait()
            return {"prices": ["0.03", "0.97"], "version": len(calls)}

        url = f"{GAMMA}/markets/123"
        tasks = [asyncio.create_task(broker.get(fetch, url)) for _ in range(10)]
        await entered.wait()
        for _ in range(3):
            await asyncio.sleep(0)
        assert len(calls) == 1 and broker.joins == 9
        release.set()
        values = await asyncio.gather(*tasks)
        assert all(x == values[0] for x in values)
        values[0]["prices"].clear()
        assert all(x["prices"] == ["0.03", "0.97"] for x in values[1:])
        assert not broker._pending
        fresh = await broker.get(fetch, url)
        assert fresh["version"] == 2 and len(calls) == 2
        await broker.close()

    asyncio.run(run())


def test_one_cancelled_account_cannot_cancel_other_observers():
    async def run():
        broker = TestPublicQueries()
        entered, release = asyncio.Event(), asyncio.Event()
        cancelled = []

        async def fetch(url, **params):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return {"fresh": True}

        first = asyncio.create_task(broker.get(fetch, CLOB_BOOK, token_id="1"))
        second = asyncio.create_task(broker.get(fetch, CLOB_BOOK, token_id="1"))
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done() and not cancelled
        release.set()
        assert await second == {"fresh": True}
        assert not cancelled and not broker._pending
        await broker.close()

    asyncio.run(run())


def test_last_waiter_cancellation_drains_request_even_if_cancelled_again():
    async def run():
        broker = TestPublicQueries()
        entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        finished = []

        async def fetch(url, **params):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleanup.set()
                await release.wait()
                finished.append(True)

        caller = asyncio.create_task(broker.get(fetch, f"{GAMMA}/markets/123"))
        await entered.wait()
        caller.cancel()
        await cleanup.wait()
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert finished == [True] and not broker._pending
        await broker.close()

    asyncio.run(run())


def test_close_drains_shared_queries_and_future_calls_fail_closed():
    async def run():
        broker = TestPublicQueries()
        entered = asyncio.Event()
        finished = []

        async def fetch(url, **params):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                finished.append(True)

        pending = asyncio.create_task(broker.get(fetch, f"{GAMMA}/markets/123"))
        await entered.wait()
        await broker.close()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert finished == [True] and not broker._pending
        with pytest.raises(RuntimeError, match="已关闭"):
            await broker.get(fetch, f"{GAMMA}/markets/123")

    asyncio.run(run())


def test_errors_are_not_cached_and_retries_perform_new_reads():
    async def run():
        broker = TestPublicQueries()
        calls = []
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(url, **params):
            calls.append(True)
            entered.set()
            await release.wait()
            if len(calls) == 1:
                raise ConnectionError("public fixture failure")
            return {"fresh": True}

        tasks = [asyncio.create_task(broker.get(fetch, f"{GAMMA}/markets/123")) for _ in range(3)]
        await entered.wait()
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(value, ConnectionError) for value in results)
        assert len(calls) == 1 and not broker._pending
        assert await broker.get(fetch, f"{GAMMA}/markets/123") == {"fresh": True}
        assert len(calls) == 2
        await broker.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "url,params",
    [
        (f"{GAMMA}/events/keyset", {}),
        ("https://example.invalid/markets/123", {}),
        (CLOB_BOOK, {"token_id": ["1"]}),
        (CLOB_BOOK + "?token_id=1", {}),
    ],
)
def test_unknown_requests_retain_original_path(url, params):
    async def run():
        broker = TestPublicQueries()
        calls = []

        async def fetch(url, **params):
            calls.append(True)
            await asyncio.sleep(0)
            return {"ok": True}

        await asyncio.gather(*(broker.get(fetch, url, **params) for _ in range(3)))
        assert len(calls) == 3 and not broker._pending
        assert broker.requests == broker.joins == 0
        await broker.close()

    asyncio.run(run())


def test_parameter_identity_and_capacity_never_suppress_public_observations():
    async def run():
        broker = TestPublicQueries(max_pending=1)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def fetch(url, **params):
            calls.append(params)
            entered.set()
            await release.wait()
            return params

        first = asyncio.create_task(broker.get(fetch, CLOB_BOOK, token_id=True))
        await entered.wait()
        second = asyncio.create_task(broker.get(fetch, CLOB_BOOK, token_id=1))
        for _ in range(3):
            await asyncio.sleep(0)
        assert len(calls) == 2 and len(broker._pending) == 1
        release.set()
        assert await first == {"token_id": True}
        assert await second == {"token_id": 1}
        await broker.close()

    asyncio.run(run())


def test_live_factory_get_is_still_original_method():
    from pm_nautilus.test_market import create_market_service
    from types import SimpleNamespace

    # No private adapter is constructed; the service factory must preserve the
    # original class for every runtime except the exact TEST implementation.
    runtime = SimpleNamespace(store=SimpleNamespace(get=lambda *args: {}))
    service = create_market_service(runtime)
    assert type(service) is MarketService
    assert type(service).get is MarketService.get
    asyncio.run(service.close())


def test_real_test_services_merge_public_get_without_merging_live(tmp_path):
    import httpx
    from pm_nautilus.strategy import Runtime
    from pm_nautilus.test_market import TestMarketHub, create_market_service
    from pm_nautilus.test_runtime import TestRuntime
    from test_lifecycle import controlled_factory

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(request)
            if len(requests) == 2:
                entered.set()
            await release.wait()
            return httpx.Response(200, json={"public": ["fresh"], "version": len(requests)})

        hub = TestMarketHub(transport=httpx.MockTransport(handler))
        runtimes = [TestRuntime(tmp_path / str(n) / "test.sqlite") for n in range(10)]
        live = Runtime(tmp_path / "live.sqlite", mode="LIVE", live_factory=controlled_factory)
        services = [create_market_service(runtime, hub=hub) for runtime in runtimes + [live]]
        tasks = []
        try:
            tasks = [
                asyncio.create_task(service.get(f"{GAMMA}/markets/123")) for service in services
            ]
            await asyncio.wait_for(entered.wait(), 3)
            assert len(requests) == 2  # One TEST network request plus LIVE's original request.
            assert hub._test_public_queries.joins == 9
            release.set()
            values = await asyncio.gather(*tasks)
            values[0]["public"].clear()
            assert all(x["public"] == ["fresh"] for x in values[1:])
            await services[0].get(f"{GAMMA}/markets/123")
            await services[-1].get(f"{GAMMA}/markets/123")
            assert len(requests) == 4  # Completed replies are never cached in either mode.
            live.client._submit_order.assert_not_awaited()
            live.client._cancel_order.assert_not_awaited()
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            for service in services:
                await service.close()
            await hub.close()
            for runtime in runtimes:
                runtime.close()
            await live.client.shutdown()
            live.store.close()

    asyncio.run(run())


def test_ten_independent_claims_advance_with_one_shared_public_observation(tmp_path):
    import httpx
    from pm_nautilus.redemption import RedemptionService
    from pm_nautilus.test_market import TestMarketHub, create_market_service
    from pm_nautilus.test_runtime import TestRuntime
    from test_redemption_scheduling import resolved
    from test_runtime import setup

    async def run():
        reference, clock, token = setup(tmp_path / "reference")
        reference.close()
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json=resolved(token))

        hub = TestMarketHub(transport=httpx.MockTransport(handler))
        runtimes, services, redeemers = [], [], []
        pending = []
        try:
            for number in range(10):
                runtime = TestRuntime(tmp_path / str(number) / "test.sqlite", clock=clock)
                runtime.add_tokens([token])
                runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
                runtime.start()
                runtime.pause()
                assert runtime.cash() == 99_000_000
                service = create_market_service(runtime, hub=hub)
                runtimes.append(runtime)
                services.append(service)
                redeemers.append(RedemptionService(runtime, service))
            pending = [asyncio.create_task(redeemer.run_once()) for redeemer in redeemers]
            await asyncio.wait_for(entered.wait(), 3)
            for _ in range(10):
                await asyncio.sleep(0)
            assert len(requests) == 1 and hub._test_public_queries.joins == 9
            release.set()
            await asyncio.gather(*pending)
            for runtime in runtimes:
                claim = runtime.business["claims"][token.condition_id]
                assert claim["state"] == "CONFIRMED"
                assert runtime.cash() == 99_000_000  # Confirmation is a separate step.
            await asyncio.gather(*(redeemer.run_once() for redeemer in redeemers))
            assert len(requests) == 1  # Already identified claims don't re-query.
            for runtime in runtimes:
                claim = runtime.business["claims"][token.condition_id]
                assert claim["state"] == "CREDITED"
                assert runtime.cash() == 149_000_000
                assert not runtime.business["cycles"]
                assert (await runtime.validate_async())["ok"]
            assert len({id(r.business["claims"]) for r in runtimes}) == 10
        finally:
            release.set()
            await asyncio.gather(*pending, return_exceptions=True)
            for service in services:
                await service.close()
            await hub.close()
            for runtime in runtimes:
                runtime.close()

    asyncio.run(run())
