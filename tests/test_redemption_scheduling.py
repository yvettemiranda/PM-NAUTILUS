"""Controlled public lookups never hold up already identified settlement rights."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from pm_nautilus.redemption import RedemptionService
from test_runtime import setup


def two_holdings(tmp_path):
    runtime, _, first = setup(tmp_path)
    second = replace(
        first, token_id="2", event_id="event-2", market_id="market-2", condition_id="c2"
    )
    runtime.add_tokens([second])
    for token in (first, second):
        runtime.book(token.token_id, [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    runtime.start()
    runtime.pause()
    assert len(runtime.business["cycles"]) == 2
    return runtime, first, second


def unclosed(token):
    return {"id": token.market_id, "conditionId": token.condition_id, "closed": False}


def resolved(token):
    return {
        "id": token.market_id,
        "conditionId": token.condition_id,
        "closed": True,
        "umaResolutionStatus": "resolved",
        "clobTokenIds": [token.token_id, "99"],
        "outcomePrices": ["1", "0"],
    }


def test_existing_claim_advances_during_unrelated_slow_lookup_once_per_round(tmp_path):
    async def run():
        runtime, first, second = two_holdings(tmp_path)
        entered, release, advanced = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def get(url):
            assert url.endswith(second.market_id)
            entered.set()
            await release.wait()
            return unclosed(second)

        market = SimpleNamespace(get=get)
        service = RedemptionService(runtime, market)
        service.identify(first, {first.token_id: 1_000_000, "99": 0})
        claim = runtime.business["claims"][first.condition_id]
        advance = service.advance

        async def observed(item):
            await advance(item)
            advanced.set()

        service.advance = observed
        task = asyncio.create_task(service.run_once())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(advanced.wait(), 2)
            assert not task.done()
            assert claim["state"] == "CONFIRMED"
            assert market.resolution_status["pendingQueryCount"] == 1
            release.set()
            await task
            assert claim["state"] == "CONFIRMED"  # No second advancement in the same round.
            await service.run_once()
            assert claim["state"] == "CREDITED"
            assert runtime.validate()["ok"]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            runtime.close()

    asyncio.run(run())


def test_newly_identified_claim_advances_without_waiting_for_other_market(tmp_path):
    async def run():
        runtime, first, second = two_holdings(tmp_path)
        release, advanced = asyncio.Event(), asyncio.Event()

        async def get(url):
            if url.endswith(first.market_id):
                return resolved(first)
            await release.wait()
            return unclosed(second)

        service = RedemptionService(runtime, SimpleNamespace(get=get))
        advance = service.advance

        async def observed(claim):
            await advance(claim)
            advanced.set()

        service.advance = observed
        task = asyncio.create_task(service.run_once())
        try:
            await asyncio.wait_for(advanced.wait(), 2)
            assert not task.done()
            assert runtime.business["claims"][first.condition_id]["state"] == "CONFIRMED"
            release.set()
            await task
            assert runtime.validate()["ok"]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            runtime.close()

    asyncio.run(run())


def test_resolution_failure_is_not_hidden_by_success_of_another_market(tmp_path):
    async def run():
        runtime, first, second = two_holdings(tmp_path)
        failed = True

        async def get(url):
            if failed and url.endswith(first.market_id):
                raise httpx.ReadTimeout("private provider URL must never be published")
            return unclosed(first if url.endswith(first.market_id) else second)

        market = SimpleNamespace(get=get)
        service = RedemptionService(runtime, market)
        try:
            for _ in range(3):
                await service.run_once()
            status = market.resolution_status
            assert status["affectedMarketCount"] == 1
            assert status["checkedMarketCount"] == 2
            assert status["consecutiveFailures"] == 3
            assert status["error"] == "ReadTimeout"
            assert status["failureSince"]
            assert "private" not in str(status)
            failure_since = status["failureSince"]
            await service.run_once()
            assert market.resolution_status["failureSince"] == failure_since
            failed = False
            await service.run_once()
            status = market.resolution_status
            assert status["error"] is None and status["affectedMarketCount"] == 0
            assert status["consecutiveFailures"] == 0 and status["failureSince"] is None
            assert status["lastSuccessAt"] and status["pendingQueryCount"] == 0
            assert runtime.store.get("settlement_error") is None
            assert runtime.validate()["ok"]
        finally:
            runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_claim_worker_and_queries_are_awaited_on_completion_or_cancellation(tmp_path, cancel):
    async def run():
        runtime, first, second = two_holdings(tmp_path)
        entered = asyncio.Event()

        async def get(url):
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            return unclosed(first if url.endswith(first.market_id) else second)

        service = RedemptionService(runtime, SimpleNamespace(get=get))
        task = asyncio.create_task(service.run_once())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
            assert not service.busy
            assert all(not row["pending"] for row in service._query_health.values())
            assert runtime.validate()["ok"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            runtime.close()

    asyncio.run(run())
